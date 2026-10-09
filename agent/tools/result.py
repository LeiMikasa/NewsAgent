"""统一成功/失败结果，错误也能回送给模型。"""

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str
    error_code: str = ""

    @classmethod
    def success(cls, value: Any) -> "ToolResult":
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return cls(True, content)

    @classmethod
    def failure(cls, code: str, message: str) -> "ToolResult":
        return cls(False, message, code)

    def model_content(self) -> str:
        if self.ok:
            return self.content
        return json.dumps({"ok": False, "error_code": self.error_code,
                           "message": self.content}, ensure_ascii=False)
