"""统一成功/失败结果，错误也能回送给模型。"""

import errno
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str
    error_code: str = ""
    field: str | None = None
    action: str = "none"
    transient: bool = False
    execution_status: str = "succeeded"
    inspected_path: str | None = None
    retry_after: float | None = None
    attempts: int = 0
    repair_failures: int = 0

    @classmethod
    def success(cls, value: Any, *, inspected_path: str | None = None) -> "ToolResult":
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return cls(True, content, inspected_path=inspected_path)

    @classmethod
    def failure(cls, code: str, message: str, *, field: str | None = None,
                action: str = "report", transient: bool = False,
                execution_status: str = "not_executed", inspected_path: str | None = None,
                retry_after: float | None = None) -> "ToolResult":
        return cls(False, message, code, field=field, action=action, transient=transient,
                   execution_status=execution_status, inspected_path=inspected_path,
                   retry_after=retry_after)

    @classmethod
    def from_exception(cls, exc: Exception, *, read_only: bool = False) -> "ToolResult":
        """Adapt HarnessAgent's classification; uncertain mutations never retry."""
        status = "failed" if read_only else "unknown"
        message = f"{type(exc).__name__}: {exc}"
        for kind, code, action in ((PermissionError, "PERMISSION_DENIED", "report"),
                                   (FileNotFoundError, "NOT_FOUND", "inspect"),
                                   (UnicodeError, "NOT_TEXT", "report"),
                                   ((IsADirectoryError, NotADirectoryError), "PRECONDITION_FAILED", "inspect")):
            if isinstance(exc, kind):
                return cls.failure(code, message, action=action, execution_status=status)
        transient = isinstance(exc, (TimeoutError, ConnectionError, InterruptedError)) or (
            isinstance(exc, OSError) and exc.errno in {
                errno.EAGAIN, errno.EINTR, errno.EBUSY, errno.ECONNRESET,
                errno.ECONNABORTED, errno.ETIMEDOUT,
            })
        return cls.failure("TIMEOUT" if isinstance(exc, TimeoutError) else
                           "TRANSIENT_ERROR" if transient else "TOOL_ERROR", message,
                           action="report" if read_only else "inspect_state",
                           transient=transient, execution_status=status)

    def model_content(self) -> str:
        if self.ok:
            return self.content
        return json.dumps({"ok": False, "error_code": self.error_code,
                           "message": self.content, "field": self.field,
                           "transient": self.transient, "auto_retry": False,
                           "next_action": self.action, "execution_status": self.execution_status,
                           "attempts": self.attempts, "repair_failures": self.repair_failures,
                           "retry_after": self.retry_after,
                           "recovery_hint": {
                               "correct_arguments": "Correct the reported arguments before calling again.",
                               "inspect": "Inspect the actual path or target before correcting the operation.",
                               "inspect_state": "Side effects may have occurred. Inspect actual state before another mutation.",
                               "choose_alternative": "Choose another source or adjust the extraction method within the repair budget.",
                               "report": "Report the blocker or choose an appropriate alternative; do not bypass permissions.",
                           }.get(self.action, "Report the failure.")}, ensure_ascii=False)
