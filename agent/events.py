"""实例级生命周期钩子；结构参照 HarnessAgent.events。"""

from dataclasses import dataclass
from typing import Any, Callable

SESSION_START = "SessionStart"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
STOP = "Stop"
SESSION_END = "SessionEnd"


@dataclass(frozen=True)
class StopDirective:
    action: str  # continue | return
    message: str = ""
    stop_reason: str = "final"

    def __post_init__(self) -> None:
        if self.action not in {"continue", "return"}:
            raise ValueError("StopDirective.action must be continue or return")
        if self.action == "continue" and not self.message.strip():
            raise ValueError("continue requires a message for the model")


class Hooks:
    def __init__(self) -> None:
        self._callbacks: dict[str, list[Callable]] = {}

    def register(self, event: str, callback: Callable) -> Callable:
        self._callbacks.setdefault(event, []).append(callback)
        return callback

    def on(self, event: str) -> Callable:
        return lambda callback: self.register(event, callback)

    def unregister(self, event: str, callback: Callable) -> None:
        callbacks = self._callbacks.get(event, [])
        if callback in callbacks:
            callbacks.remove(callback)

    def callbacks(self, event: str) -> list[Callable]:
        return list(self._callbacks.get(event, ()))

    def trigger(self, event: str, *args: Any) -> Any:
        """按注册顺序调用，首个非 None 值即为拦截/控制结果。"""
        for callback in tuple(self._callbacks.get(event, ())):
            result = callback(*args)
            if result is not None:
                return result
        return None

    def collect(self, event: str, *args: Any) -> list[Any]:
        """通知所有观察者，不因返回值而短路。异常交给调用方处理。"""
        results = []
        for callback in tuple(self._callbacks.get(event, ())):
            result = callback(*args)
            if result is not None:
                results.append(result)
        return results
