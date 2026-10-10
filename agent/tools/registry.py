"""模型看到的 schema 与执行 handler 使用同一工具注册表。"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource

from ..config import Settings
from .result import ToolResult


def _reject_reference(uri: str):
    raise NoSuchResource(ref=uri)


@dataclass
class ToolContext:
    settings: Settings
    workdir: Path
    runtime: Any = None
    extra: dict = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict, ToolContext], ToolResult]
    read_only: bool = False
    retry_safe: bool = False  # 由宿主显式允许；只读标记本身不启用重试。

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": copy.deepcopy(self.input_schema)}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._validators: dict[str, Draft202012Validator] = {}
    # 注册工具时，先检查名称是否重复，Schema是否合法，然后创建对应的JSON Schema校验器
    def register(self, tool: Tool, *, replace: bool = False) -> Tool:
        if not isinstance(tool.name, str) or not tool.name:
            raise ValueError("tool name must be a nonempty string")
        if tool.name in self._tools and not replace:
            raise ValueError(f"tool already registered: {tool.name}")
        Draft202012Validator.check_schema(tool.input_schema)
        # 冻结注册时的 schema，调用方之后修改定义不应改变校验规则。
        registered = copy.copy(tool)
        registered.input_schema = copy.deepcopy(tool.input_schema)
        self._validators[tool.name] = Draft202012Validator(
            registered.input_schema, registry=Registry(retrieve=_reject_reference))
        self._tools[tool.name] = registered
        return registered

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def definitions(self) -> list[dict]:
        return [tool.definition() for tool in self._tools.values()]
    # 参数校验
    def validate(self, name: Any, args: Any) -> ToolResult | None:
        # 先校验工具名称是否存在
        if not isinstance(name, str) or name not in self._tools:
            return ToolResult.failure("UNKNOWN_TOOL", f"unknown tool {name!r}; available: {self.names()}",
                                      field="name", action="correct_arguments")
        # 检查参数必须是字典
        if not isinstance(args, dict):
            return ToolResult.failure("INVALID_ARGUMENT", "tool input must be a JSON object", action="correct_arguments")
        # 第三层按照 Schema 检查具体字段
        try:
            json.dumps(args, allow_nan=False)
            error = next(self._validators[name].iter_errors(args), None)
        except (ValueError, TypeError) as exc:
            return ToolResult.failure("INVALID_ARGUMENT", str(exc), action="correct_arguments")
        except Exception as exc:
            return ToolResult.failure("INVALID_SCHEMA", str(exc))
        if error is not None:
            field_name = ".".join(str(part) for part in error.path) or "input"
            return ToolResult.failure("INVALID_ARGUMENT", f"{field_name}: {error.message}",
                                      field=field_name, action="correct_arguments")
        return None

    def dispatch(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        invalid = self.validate(name, args)
        if invalid is not None:
            return invalid
        try:
            value = self._tools[name].handler(copy.deepcopy(args), ctx)
            return value if isinstance(value, ToolResult) else ToolResult.success(value)
        except Exception as exc:
            return ToolResult.from_exception(exc, read_only=self._tools[name].read_only)
