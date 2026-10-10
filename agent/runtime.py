"""组装配置、模型、工具、hooks，并持有跨用户请求的会话。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .briefs import BriefStore, SHANGHAI
from .config import Settings, load_settings
from .events import Hooks, SESSION_END, SESSION_START, STOP, USER_PROMPT_SUBMIT
from .llm import LLMClient, MockLLM, build_client
from .loop import LoopResult, execute_tool, run_loop
from .memory import MemoryStore
from .prompt import build_system_prompt
from .tools import ToolContext, ToolRegistry, ToolResult, ToolRecovery, register_basic_tools, register_news_tools


@dataclass
class RuntimeStats:
    requests: int = 0
    turns: int = 0
    tool_calls: int = 0
    tool_attempts: int = 0


@dataclass
class BriefResult:
    result: LoopResult
    brief: dict | None = None
    save_error: str = ""

    @property
    def ok(self) -> bool:
        return self.result.ok and self.brief is not None and not self.save_error


class Runtime:
    def __init__(self, settings: Settings, llm: LLMClient, *, hooks: Hooks | None = None,
                 tools: ToolRegistry | None = None, on_event: Callable[[str], None] | None = None,
                 memory_llm: LLMClient | None = None):
        self.settings, self.llm = settings, llm
        self.hooks = hooks if hooks is not None else Hooks()
        self.tools = tools if tools is not None else ToolRegistry()
        if tools is None:
            register_basic_tools(self.tools)
            register_news_tools(self.tools)
        self.messages: list[dict] = []
        self.stats = RuntimeStats()
        self.ctx = ToolContext(settings, settings.workdir, runtime=self)
        self.recovery = ToolRecovery(max_retries=settings.max_tool_retries, max_repairs=settings.max_tool_repairs)
        self.on_event = on_event
        self.briefs = BriefStore(settings.workdir, settings.briefs_dir,
                                 max_content_bytes=settings.max_file_bytes)
        # Separate offline bookkeeping from the agent's scripted conversation.
        # Live runtimes reuse the configured client unless explicitly overridden.
        self.memory_llm = memory_llm if memory_llm is not None else (
            MockLLM(responder=lambda index, messages, tools:
                    "offline selection" if messages[0]["content"].startswith("Select memory records")
                    else "[]") if settings.provider == "mock" else llm)
        self.memory = MemoryStore(self.memory_llm, settings.model, settings.memory_dir,
                                  settings.workdir, enabled=settings.memory_enabled)
        self._memory_sections: list[str] | None = None
        self._memory_pending = False
        self.hooks.register(SESSION_START, self._memory_session_start)
        self.hooks.register(STOP, self._memory_stop_hook)
        self._started = False
        self._closed = False
        self._busy = False

    @classmethod
    def create(cls, settings: Settings | None = None, *, llm: LLMClient | None = None,
               script: list | None = None, responder: Callable | None = None,
               env_file=None, hooks: Hooks | None = None, tools: ToolRegistry | None = None,
               on_event: Callable[[str], None] | None = None,
               memory_llm: LLMClient | None = None, **overrides) -> "Runtime":
        settings = settings if settings is not None else load_settings(env_file, **overrides)
        if llm is not None and (script is not None or responder is not None):
            raise ValueError("use either llm or script/responder")
        client = llm if llm is not None else build_client(settings, script=script, responder=responder)
        return cls(settings, client, hooks=hooks, tools=tools, on_event=on_event, memory_llm=memory_llm)

    def _memory_session_start(self, ctx: ToolContext) -> None:
        if not self.memory.enabled:
            return
        try:
            if not self.memory.read_index() and self.memory.list_records():
                self.memory.rebuild_index()
            ctx.extra["missing_profile_fields"] = self.memory.missing_profile_fields()
        except Exception as exc:
            self.memory._note(f"startup memory skipped: {type(exc).__name__}: {exc}")

    def _memory_stop_hook(self, result: LoopResult, ctx: ToolContext) -> None:
        # Stop may be vetoed by a later hook. Only extract after the request
        # actually finishes, once, so continued/failed rounds cannot write memory.
        if self.memory.enabled and self.settings.memory_auto_extract:
            self._memory_pending = True

    @property
    def missing_profile_fields(self) -> list[str]:
        return self.memory.missing_profile_fields()

    def update_profile(self, *, identity: str | None = None, preferences: str | None = None) -> dict[str, str]:
        if self._closed or self._busy:
            raise RuntimeError("cannot update profile while runtime is closed or busy")
        profile = self.memory.set_profile(identity=identity, preferences=preferences)
        self._memory_sections = None
        self.ctx.extra["missing_profile_fields"] = self.memory.missing_profile_fields()
        self.ctx.extra.pop("profile_setup_skipped", None)
        return profile

    def build_system(self) -> str:
        base = build_system_prompt(self.settings)
        if not self.memory.enabled:
            return base
        if self._memory_sections is None:
            try:
                self._memory_sections = self.memory.system_sections(self.messages)
            except Exception as exc:
                self.memory._note(f"memory recall skipped: {type(exc).__name__}: {exc}")
                self._memory_sections = []
        sections = [base, *self._memory_sections]
        missing = self.memory.missing_profile_fields()
        if missing:
            labels = {"identity": "职业/角色与背景", "preferences": "新闻关注主题与阅读偏好"}
            instruction = ("本次会话已跳过资料设置，请使用已有信息或通用简报，不重复追问。"
                           if self.ctx.extra.get("profile_setup_skipped") else
                           "生成个性化新闻简报前请了解缺少的资料；用户可明确跳过。其他任务正常处理。")
            sections.append("尚未设置用户长期资料：" + "、".join(labels[field] for field in missing) + "。" + instruction)
        sections.append("长期身份与新闻偏好通过记忆模块保存。用户说‘今天/这次’时只影响当前任务；"
                        "说‘以后/长期’或明确更正身份时才更新长期记录。不要用文件工具自行改写记忆索引。")
        if not self.settings.memory_auto_extract:
            sections.append("本次运行关闭了自动记忆提取。长期资料变更请提示用户使用 /profile edit；"
                            "不要声称已自动保存对话里的新偏好。")
        return "\n\n".join(sections)

    def _finish_memory(self, messages: list[dict]) -> None:
        try:
            if self.memory.extract(messages):
                self.memory.consolidate()
                self.ctx.extra["missing_profile_fields"] = self.memory.missing_profile_fields()
        except Exception as exc:
            self.memory._note(f"memory extraction skipped: {type(exc).__name__}: {exc}")

    def _start(self) -> None:
        if self._closed:
            raise RuntimeError("runtime is closed")
        if not self._started:
            self.hooks.collect(SESSION_START, self.ctx)
            self._started = True

    def submit(self, prompt: str) -> LoopResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be nonempty text")
        if self._busy:
            raise RuntimeError("runtime is already processing a request")
        self._busy = True
        self.stats.requests += 1
        try:
            self._start()
            blocked = self.hooks.trigger(USER_PROMPT_SUBMIT, prompt, self.ctx)
            if blocked is not None:
                return LoopResult(stop_reason="blocked", error=str(blocked))
            request_start = len(self.messages)
            self.recovery = ToolRecovery(max_retries=self.settings.max_tool_retries,
                                         max_repairs=self.settings.max_tool_repairs)
            self._memory_sections = None
            self._memory_pending = False
            self.messages.append({"role": "user", "content": prompt})
            result = run_loop(llm=self.llm, registry=self.tools, messages=self.messages,
                              system=self.build_system, ctx=self.ctx,
                              hooks=self.hooks, max_turns=self.settings.max_turns, on_event=self.on_event,
                              recovery=self.recovery)
            self.stats.turns += result.turns
            self.stats.tool_calls += result.tool_calls
            self.stats.tool_attempts += result.tool_attempts
            if result.ok and self._memory_pending:
                self._finish_memory(self.messages[request_start:])
                self._memory_sections = None
            return result
        except Exception as exc:
            return LoopResult(stop_reason="error", error=f"{type(exc).__name__}: {exc}")
        finally:
            self._busy = False

    def generate_brief(self, prompt: str | None = None, *, title: str | None = None) -> BriefResult:
        """Generate via the existing loop, then archive its completed final text."""
        if self._closed or self._busy:
            raise RuntimeError("cannot generate a brief while runtime is closed or busy")
        if self.memory.enabled and self.missing_profile_fields and not self.ctx.extra.get("profile_setup_skipped"):
            return BriefResult(LoopResult(stop_reason="blocked",
                error="请先通过 --setup-profile 或 /profile edit 设置身份和偏好；通用简报可用 --no-memory 或交互时 /skip。"))
        if (isinstance(self.llm, MockLLM) and self.llm.script is None and self.llm.responder is None):
            return BriefResult(LoopResult(stop_reason="blocked",
                error="默认 mock 只返回框架状态，无法生成简报。请配置真实模型，或使用 mock script/responder 做离线测试。"))
        request = (f"当前日期为 {datetime.now(SHANGHAI):%Y-%m-%d}（Asia/Shanghai）。"
                   "请根据我的身份和长期订阅偏好生成今日新闻简报，按我订阅的主题检索和筛选新闻，给出来源链接和日期，"
                   "区分报道事实与分析。" if prompt is None else prompt)
        result = self.submit(request)
        generated = BriefResult(result)
        if not result.ok:
            return generated
        self._busy = True
        try:
            generated.brief = self.briefs.save(result.text, title=title,
                                               provider=self.settings.provider, model=self.settings.model,
                                               turns=result.turns, tool_calls=result.tool_calls,
                                               tool_attempts=result.tool_attempts)
        except (OSError, ValueError, UnicodeError, RuntimeError) as exc:
            generated.save_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._busy = False
        return generated

    def call_tool(self, name: str, **args) -> ToolResult:
        """直接调用仍走相同的参数校验与 Pre/PostToolUse 链路。"""
        if self._busy:
            raise RuntimeError("runtime is already processing a request")
        self._busy = True
        initial_executions = self.recovery.executions
        try:
            self._start()
            self.stats.tool_calls += 1
            return execute_tool({"type": "tool_use", "name": name, "input": args},
                                self.tools, self.ctx, self.hooks, self.recovery, self.on_event)
        finally:
            self.stats.tool_attempts += self.recovery.executions - initial_executions
            self._busy = False

    def reset(self) -> None:
        if self._busy:
            raise RuntimeError("cannot reset an active request")
        self.messages.clear()
        self.ctx.extra.clear()
        self.recovery = ToolRecovery(max_retries=self.settings.max_tool_retries,
                                     max_repairs=self.settings.max_tool_repairs)
        self._memory_sections = None
        self._memory_pending = False

    def close(self) -> None:
        if self._closed:
            return
        if self._busy:
            raise RuntimeError("cannot close an active request")
        self._closed = True
        try:
            if self._started:
                self.hooks.collect(SESSION_END, self.ctx)
        finally:
            try:
                close_client = getattr(self.llm, "close", None)
                if callable(close_client):
                    close_client()
            finally:
                if self.memory_llm is not self.llm:
                    close_memory = getattr(self.memory_llm, "close", None)
                    if callable(close_memory):
                        close_memory()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *_args) -> None:
        self.close()
