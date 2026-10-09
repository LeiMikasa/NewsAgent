"""组装配置、模型、工具、hooks，并持有跨用户请求的会话。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .config import Settings, load_settings
from .events import Hooks, SESSION_END, SESSION_START, USER_PROMPT_SUBMIT
from .llm import LLMClient, build_client
from .loop import LoopResult, execute_tool, run_loop
from .prompt import build_system_prompt
from .tools import ToolContext, ToolRegistry, ToolResult, register_basic_tools


@dataclass
class RuntimeStats:
    requests: int = 0
    turns: int = 0
    tool_calls: int = 0


class Runtime:
    def __init__(self, settings: Settings, llm: LLMClient, *, hooks: Hooks | None = None,
                 tools: ToolRegistry | None = None, on_event: Callable[[str], None] | None = None):
        self.settings, self.llm = settings, llm
        self.hooks = hooks if hooks is not None else Hooks()
        self.tools = tools if tools is not None else ToolRegistry()
        if tools is None:
            register_basic_tools(self.tools)
        self.messages: list[dict] = []
        self.stats = RuntimeStats()
        self.ctx = ToolContext(settings, settings.workdir, runtime=self)
        self.on_event = on_event
        self._started = False
        self._closed = False
        self._busy = False

    @classmethod
    def create(cls, settings: Settings | None = None, *, llm: LLMClient | None = None,
               script: list | None = None, responder: Callable | None = None,
               env_file=None, hooks: Hooks | None = None, tools: ToolRegistry | None = None,
               on_event: Callable[[str], None] | None = None, **overrides) -> "Runtime":
        settings = settings if settings is not None else load_settings(env_file, **overrides)
        if llm is not None and (script is not None or responder is not None):
            raise ValueError("use either llm or script/responder")
        client = llm if llm is not None else build_client(settings, script=script, responder=responder)
        return cls(settings, client, hooks=hooks, tools=tools, on_event=on_event)

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
            self.messages.append({"role": "user", "content": prompt})
            result = run_loop(llm=self.llm, registry=self.tools, messages=self.messages,
                              system=lambda: build_system_prompt(self.settings), ctx=self.ctx,
                              hooks=self.hooks, max_turns=self.settings.max_turns, on_event=self.on_event)
            self.stats.turns += result.turns
            self.stats.tool_calls += result.tool_calls
            return result
        except Exception as exc:
            return LoopResult(stop_reason="error", error=f"{type(exc).__name__}: {exc}")
        finally:
            self._busy = False

    def call_tool(self, name: str, **args) -> ToolResult:
        """直接调用仍走相同的参数校验与 Pre/PostToolUse 链路。"""
        if self._busy:
            raise RuntimeError("runtime is already processing a request")
        self._busy = True
        try:
            self._start()
            self.stats.tool_calls += 1
            return execute_tool({"type": "tool_use", "name": name, "input": args},
                                self.tools, self.ctx, self.hooks)
        finally:
            self._busy = False

    def reset(self) -> None:
        if self._busy:
            raise RuntimeError("cannot reset an active request")
        self.messages.clear()
        self.ctx.extra.clear()

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
            close_client = getattr(self.llm, "close", None)
            if callable(close_client):
                close_client()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *_args) -> None:
        self.close()
