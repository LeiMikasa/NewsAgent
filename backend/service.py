"""Manual and scheduled requests share one worker and one control lock."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Callable

from agent import BriefStore, Runtime, Settings
from agent.briefs import SHANGHAI
from agent.llm import MockLLM
from agent.memory import MemoryStore

from .storage import InstanceLock, StateStore
from .email_delivery import EmailSender

logger = logging.getLogger(__name__)
CATCH_UP_WINDOW = timedelta(hours=1)


class BusyError(RuntimeError):
    pass


class ProfileRequired(ValueError):
    pass


class BackendService:
    def __init__(self, settings: Settings, *, runtime_factory: Callable | None = None,
                 email_sender: EmailSender | None = None):
        self.settings = settings
        self.store = StateStore(settings.workdir) # 保存订阅设置，任务状态，错误和邮件结果
        # Operational state must not be mistaken for long-term memory or a brief.
        if any(self.store.path.resolve().is_relative_to(directory) for directory in (settings.memory_dir, settings.briefs_dir)):
            raise ValueError("backend state directory overlaps memory_dir or briefs_dir")
        self.instance_lock = InstanceLock(self.store.path.with_suffix(".lock"))
        # Profile CRUD is deterministic and must work without API credentials or model calls.
        self.memory = MemoryStore(MockLLM(script=[]), settings.model, settings.memory_dir,
                                  settings.workdir, enabled=settings.memory_enabled)
        self.briefs = BriefStore(settings.workdir, settings.briefs_dir, max_content_bytes=settings.max_file_bytes)
        self.runtime_factory = runtime_factory or (lambda on_event: Runtime.create(settings, on_event=on_event))
        self.email_sender = email_sender if email_sender is not None else EmailSender(settings)
        self._lock = threading.RLock()
        self._executor = None
        self._active_id = None
        self._started = False
        self._closing = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("backend already started")
            self.store.prepare()  # 检查路径并创建数据库父目录
            self.instance_lock.acquire()  # 获取操作系统文件锁，阻止另一后端使用同一工作区
            try:
                self.store.initialize()   # 创建表
                self.store.recover_interrupted()
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="newsagent")
                self._started = True
                self._closing = False
            except Exception:
                self.instance_lock.release()
                raise

    def close(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._closing = True
            executor = self._executor
        # Do not hold the control lock while the worker finishes its current request.
        try:
            executor.shutdown(wait=True)
        finally:
            with self._lock:
                self._started = False
                self._executor = None
                self.instance_lock.release()

    def clean_error(self, message: str) -> str:
        for secret in (self.settings.api_key, self.settings.tavily_api_key, self.settings.smtp_password):
            if secret:
                message = message.replace(secret, "[redacted]")
        return message[:16_000]
    # 查看身份和偏好
    def profile(self) -> dict:
        with self._lock:
            return {**self.memory.profile(), "missing_fields": self.memory.missing_profile_fields(),
                    "memory_enabled": self.memory.enabled}

    def update_profile(self, *, identity: str | None = None, preferences: str | None = None) -> dict:
        with self._lock:
            if self._active_id is not None:
                raise BusyError("后台任务正在执行，请完成后再修改资料。")
            self.memory.set_profile(identity=identity, preferences=preferences)
            return self.profile()

    def require_profile(self) -> None:
        if self.memory.enabled and self.memory.missing_profile_fields():
            raise ProfileRequired("请先填写身份和新闻偏好。")
    # 保存订阅并更新定时器
    def update_subscription(self, values: dict, apply_schedule: Callable) -> dict:
        with self._lock:
            if values["enabled"]:
                self.require_profile()
            old = self.store.subscription()  # 先读旧设置，供失败回滚用
            saved = self.store.set_subscription(**values)
            try:
                apply_schedule(saved)
            except Exception:
                self.store.set_subscription(**{key: old[key] for key in ("enabled", "time", "timezone")})
                apply_schedule(old)
                raise
            return saved

    def status(self) -> dict:
        with self._lock:
            active = self.store.get_run(self._active_id) if self._active_id is not None else None
            return {"busy": self._active_id is not None, "active_run": active}
    # 接收并提交生成任务
    def generate(self, *, source: str = "manual", prompt: str | None = None, title: str | None = None,
                 day: str | None = None) -> dict:
        with self._lock:
            if not self._started or self._closing:
                raise RuntimeError("backend is stopping or not started")
            if source == "scheduled" and self.store.has_scheduled_run(day):
                return {"status": "already_attempted", "scheduled_date": day}
            if self._active_id is not None:
                raise BusyError("已有生成或邮件任务正在执行，请等待完成。")
            self.require_profile()
            run = self.store.create_run(source, day=day)
            self._active_id = run["id"]
            try:
                self._executor.submit(self._generate, run["id"], prompt, title)
            except Exception as exc:
                self._active_id = None
                self.store.finish_run(run["id"], ok=False, brief_id=None,
                                      error=self.clean_error(str(exc)), result=None)
                raise
            return run

    def _generate(self, run_id: str, prompt: str | None, title: str | None) -> None:
        try:
            run = self.store.get_run(run_id)
            self.store.start_run(run_id)
            def progress(message):
                self.store.progress(run_id, self.clean_error(str(message))[:1000])
            with self.runtime_factory(progress) as runtime:
                generated = runtime.generate_brief(prompt, title=title)
            result = asdict(generated.result)
            # Successful body lives in BriefStore; retain failed-save text for recovery without new API calls.
            text = result.pop("text")
            result["error"] = self.clean_error(result["error"])
            result["save_error"] = self.clean_error(generated.save_error)
            if generated.result.ok and generated.save_error:
                result["unsaved_content"] = text
            error = self.clean_error(generated.save_error or generated.result.error or
                                     ("任务未完成：" + generated.result.stop_reason if not generated.ok else ""))
            email_pending = generated.ok and run["source"] == "scheduled" and self.settings.email_enabled
            self.store.finish_run(run_id, ok=generated.ok,
                                  brief_id=generated.brief["id"] if generated.brief else None,
                                  error=error, result=result, email_pending=email_pending)
            if email_pending:
                self._send_email(run_id)
        except Exception as exc:
            error = self.clean_error(f"{type(exc).__name__}: {exc}")
            logger.error("brief run %s failed: %s", run_id, error)
            self.store.finish_run(run_id, ok=False, brief_id=None, error=error, result=None)
        finally:
            with self._lock:
                self._active_id = None

    def send_email(self, run_id: str) -> dict:
        """Explicit send/retry uses the archived brief and never invokes the model."""
        with self._lock:
            if not self._started or self._closing:
                raise RuntimeError("backend is stopping or not started")
            if self._active_id is not None:
                raise BusyError("已有生成或邮件任务正在执行，请等待完成。")
            run = self.store.get_run(run_id)
            if run["status"] != "succeeded" or not run["brief_id"]:
                raise ValueError("这条运行记录没有成功保存的简报，无法发送邮件。")
            if run["email_status"] == "sent":
                raise ValueError("该简报已发送成功，无需重复发送。")
            self.email_sender.require_ready()
            self.briefs.get(run["brief_id"])
            run = self.store.queue_email(run_id)
            self._active_id = run_id
            try:
                self._executor.submit(self._send_email_task, run_id)
            except Exception as exc:
                self._active_id = None
                self.store.finish_email(run_id, "failed", self.clean_error(str(exc)))
                raise
            return run

    def _send_email_task(self, run_id: str) -> None:
        try:
            self._send_email(run_id)
        finally:
            with self._lock:
                self._active_id = None

    def _send_email(self, run_id: str) -> None:
        # Notification errors must not overwrite the successful generation record.
        result = None
        submitting = False
        try:
            run = self.store.get_run(run_id)
            message_id = run["email_message_id"] or self.email_sender.message_id(run_id)
            self.store.start_email(run_id, message_id, self.email_sender.recipient)
            brief = self.briefs.get(run["brief_id"])
            submitting = True
            result = self.email_sender.send(brief, message_id)
            self.store.finish_email(run_id, result.status, self.clean_error(result.error))
        except Exception as exc:
            error = self.clean_error(f"{type(exc).__name__}: {exc}")
            logger.error("email for run %s failed: %s", run_id, error)
            status = result.status if result is not None else ("unknown" if submitting else "failed")
            try:
                self.store.finish_email(run_id, status, error)
            except Exception as storage_exc:
                logger.error("email status could not be saved: %s", self.clean_error(str(storage_exc)))

    def run_scheduled(self, *, now: datetime | None = None) -> dict:
        # Lock subscription check and reservation together, so disable cannot race a new run.
        with self._lock:
            now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
            day = now.strftime("%Y-%m-%d")
            subscription = self.store.subscription()
            if not subscription["enabled"]:
                return {"status": "disabled"}
            hour, minute = map(int, subscription["time"].split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if not timedelta(0) <= now - due <= CATCH_UP_WINDOW:
                return {"status": "outside_window"}
            try:
                return self.generate(source="scheduled", day=day)
            except (BusyError, ProfileRequired) as exc:
                return self.store.create_run("scheduled", day=day, status="skipped", error=str(exc))

    def catch_up(self, *, now: datetime | None = None) -> dict | None:
        with self._lock:
            subscription = self.store.subscription()
            if not subscription["enabled"]:
                return None
            now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
            hour, minute = map(int, subscription["time"].split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if timedelta(0) <= now - due <= CATCH_UP_WINDOW:
                return self.run_scheduled(now=now)
            return None
