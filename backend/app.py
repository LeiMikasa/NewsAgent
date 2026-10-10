"""FastAPI lifecycle owns the worker and scheduler; handlers never run the model."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from contextlib import asynccontextmanager
from typing import Callable, Literal

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from agent import Settings, load_settings
from agent.memory import PROFILE_CHAR_LIMIT

from .scheduling import DailyScheduler
from .service import BackendService, BusyError, ProfileRequired
from .email_delivery import EmailSender
from .frontend import mount_frontend


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProfileUpdate(InputModel):
    identity: str | None = Field(default=None, max_length=PROFILE_CHAR_LIMIT)
    preferences: str | None = Field(default=None, max_length=PROFILE_CHAR_LIMIT)

    @field_validator("identity", "preferences")
    @classmethod
    def clean_text(cls, value):
        if value is not None:
            value = value.strip()
            if not value or "\x00" in value:
                raise ValueError("资料不能为空或包含 NUL 字符")
        return value

    @model_validator(mode="after")
    def at_least_one_field(self):
        if self.identity is None and self.preferences is None:
            raise ValueError("至少填写身份或新闻偏好中的一项")
        return self

# 订阅请求体
class SubscriptionUpdate(InputModel):
    enabled: StrictBool
    time: str
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"

    @field_validator("time")
    @classmethod
    def valid_time(cls, value):
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("订阅时间必须为 HH:MM，例如 08:30")
        return value

# 自定义请求与标题
class GenerateRequest(InputModel):
    prompt: str | None = Field(default=None, max_length=4000)
    title: str | None = Field(default=None, max_length=200)

    @field_validator("prompt", "title")
    @classmethod
    def nonempty(cls, value):
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("内容不能为空或包含 NUL 字符")
        return value

    @field_validator("title")
    @classmethod
    def single_line(cls, value):
        if value is not None and any(ord(char) < 32 for char in value):
            raise ValueError("标题必须为单行文字")
        return value

# 规定任务记录的返回格式
class RunResponse(BaseModel):
    id: str
    source: Literal["manual", "scheduled"]
    scheduled_date: str | None
    status: Literal["queued", "running", "succeeded", "failed", "interrupted", "skipped"]
    created_at: str
    started_at: str | None
    finished_at: str | None
    progress: str
    brief_id: str | None
    error: str
    result: dict | None
    email_status: Literal["not_requested", "pending", "sending", "sent", "failed", "unknown"]
    email_attempts: int
    email_error: str
    email_sent_at: str | None
    email_message_id: str | None
    email_recipient: str | None


def create_app(settings: Settings | None = None, *, runtime_factory: Callable | None = None,
               cors_origins: list[str] | None = None, email_sender: EmailSender | None = None) -> FastAPI:
    settings = settings if settings is not None else load_settings()
    service = BackendService(settings, runtime_factory=runtime_factory, email_sender=email_sender)
    scheduler = DailyScheduler(service)
# 管理应用启动和关闭
    @asynccontextmanager
    async def lifespan(app):
        service.start()
        try:
            scheduler.start()
            yield
        finally:
            try:
                scheduler.close()
            finally:
                await asyncio.to_thread(service.close)
    # 创建app
    app = FastAPI(title="NewsAgent 单用户后端", version="0.1.0", lifespan=lifespan)
    # 把service和scheduler挂到app上
    app.state.service = service
    app.state.scheduler = scheduler
    app.add_middleware(CORSMiddleware,
        allow_origins=cors_origins if cors_origins is not None else [
            "http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["GET", "PUT", "POST"], allow_headers=["Content-Type"])

    def error_response(code: str, message: str, status: int):
        return JSONResponse(status_code=status, content={"detail": {"code": code, "message": message}})

    @app.exception_handler(BusyError)
    async def busy_error(request: Request, exc: BusyError):
        return error_response("BUSY", str(exc), 409)

    @app.exception_handler(ProfileRequired)
    async def profile_required(request: Request, exc: ProfileRequired):
        return error_response("PROFILE_REQUIRED", str(exc), 409)

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError):
        return error_response("INVALID_VALUE", service.clean_error(str(exc)), 422)

    @app.exception_handler(FileNotFoundError)
    @app.exception_handler(KeyError)
    async def not_found(request: Request, exc):
        return error_response("NOT_FOUND", "未找到记录。", 404)

    @app.exception_handler(PermissionError)
    async def invalid_path(request: Request, exc):
        return error_response("INVALID_PATH", "存储路径无效。", 400)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request: Request, exc):
        return error_response("STORAGE_ERROR", "读取或写入后端状态失败。", 500)

    @app.get("/api/profile", summary="查看身份和新闻偏好")
    def get_profile():
        return service.profile()

    @app.put("/api/profile", summary="保存长期身份或新闻偏好")
    def update_profile(body: ProfileUpdate):
        return service.update_profile(**body.model_dump(exclude_none=True))

    @app.get("/api/subscription", summary="查看订阅时间和下一次执行时间")
    def get_subscription():
        return {**service.store.subscription(), "next_run_at": scheduler.next_run_at()}

    @app.put("/api/subscription", summary="启用、暂停或修改每天的订阅时间")
    def update_subscription(body: SubscriptionUpdate):
        saved = service.update_subscription(body.model_dump(), scheduler.apply)
        return {**saved, "next_run_at": scheduler.next_run_at()}

    @app.post("/api/generate", status_code=202, response_model=RunResponse,
              summary="提交手动生成任务；返回编号后轮询运行状态")
    def generate(body: GenerateRequest):
        return service.generate(prompt=body.prompt, title=body.title)

    @app.get("/api/status", summary="查看当前是否在生成及正在执行的任务")
    def get_status():
        return service.status()

    @app.get("/api/runs", response_model=list[RunResponse], summary="查看最近的任务执行记录")
    def get_runs(limit: int = Query(default=20, ge=1, le=100)):
        return service.store.list_runs(limit)

    @app.get("/api/runs/{run_id}", response_model=RunResponse, summary="查询任务进度、失败原因和简报编号")
    def get_run(run_id: str):
        return service.store.get_run(run_id)

    @app.get("/api/email", summary="查看邮件推送配置是否就绪，不返回授权码")
    def get_email():
        return service.email_sender.config()

    @app.post("/api/runs/{run_id}/send-email", status_code=202, response_model=RunResponse,
              summary="发送或重试发送已有简报，不重新生成正文")
    def send_email(run_id: str):
        return service.send_email(run_id)

    @app.get("/api/briefs", summary="分页查看简报历史，before 使用上一页最后一条编号")
    def get_briefs(limit: int = Query(default=20, ge=1, le=100), before: str | None = None):
        return service.briefs.list(limit=limit, before=before)

    @app.get("/api/briefs/{brief_id}", summary="读取一份历史简报的元信息和 Markdown 正文")
    def get_brief(brief_id: str):
        return service.briefs.get(brief_id)

    mount_frontend(app)
    return app
