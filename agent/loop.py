"""用户消息 -> 模型 -> 工具 -> 工具结果 -> 模型，直到最终回复。"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable

from .events import Hooks, POST_TOOL_USE, PRE_TOOL_USE, STOP, StopDirective
from .llm import LLMClient, normalize_response
from .tools.registry import ToolContext, ToolRegistry
from .tools.result import ToolResult


@dataclass
class LoopResult:
    text: str = ""
    turns: int = 0
    tool_calls: int = 0
    stop_reason: str = "final"  # final | max_turns | error | blocked | incomplete
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.stop_reason == "final"


def execute_tool(block: dict, registry: ToolRegistry, ctx: ToolContext, hooks: Hooks) -> ToolResult:
    name, args = block.get("name"), block.get("input")
    result = registry.validate(name, args)
    if result is None:
        try:
            blocked = hooks.trigger(PRE_TOOL_USE, copy.deepcopy(block), ctx)
            if blocked is not None:
                # PreToolUse 的任何非 None 返回值都是否决，不能伪造成成功。
                result = ToolResult.failure("POLICY_BLOCKED", str(blocked.content)
                                            if isinstance(blocked, ToolResult) else str(blocked))
            else:
                result = registry.dispatch(name, args, ctx)
        except Exception as exc:
            result = ToolResult.failure("HOOK_ERROR", f"PreToolUse: {type(exc).__name__}: {exc}")
    # 不盲目重试，尤其是有副作用的 write_file / bash。
    limit = ctx.settings.max_tool_output
    if len(result.content) > limit:
        result = ToolResult(result.ok, result.content[:limit] + "\n[output truncated]", result.error_code)
    # PostToolUse 为观察者。失败不能掩盖已经发生的工具执行结果。
    for callback in hooks.callbacks(POST_TOOL_USE):
        try:
            callback(copy.deepcopy(block), result, ctx)
        except Exception as exc:
            ctx.extra.setdefault("hook_errors", []).append(f"PostToolUse: {type(exc).__name__}: {exc}")
    return result


def run_loop(*, llm: LLMClient, registry: ToolRegistry, messages: list[dict],
             system: str | Callable[[], str], ctx: ToolContext, hooks: Hooks | None = None,
             max_turns: int = 30, on_event: Callable[[str], None] | None = None) -> LoopResult:
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0:
        raise ValueError("max_turns must be a positive integer")
    hooks = hooks if hooks is not None else Hooks()
    result = LoopResult()
    for turn in range(1, max_turns + 1):
        result.turns = turn
        try:
            if on_event:
                on_event(f"[model] round {turn}")
            response = normalize_response(llm.create(
                system=system() if callable(system) else system,
                messages=copy.deepcopy(messages), tools=registry.definitions(),
                max_tokens=ctx.settings.max_tokens))
            calls = [block for block in response.content if block.get("type") == "tool_use"]
            ids = [block.get("id") for block in calls]
            if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
                raise ValueError("tool_use ids must be nonempty and unique within a response")
            # 截断的响应可能包含不完整指令，不执行其中的任何工具。
            if response.stop_reason in {"max_tokens", "pause_turn"}:
                result.text = response.text()
                result.stop_reason = "incomplete"
                result.error = f"model response was incomplete: {response.stop_reason}"
                return result
            if response.stop_reason == "tool_use" and not calls:
                raise ValueError("tool_use stop_reason without tool_use blocks")
            if response.stop_reason == "refusal":
                result.text = response.text()
                result.stop_reason = "blocked"
                return result
            if not response.content:
                raise ValueError("model returned empty content")
            if not calls and not response.text().strip():
                raise ValueError("model returned no final text or tool calls")
            messages.append({"role": "assistant", "content": response.content})
            if calls:
                outputs = []
                for block in calls:
                    # trace 失败也不能让已追加的 assistant 留下缺失的 tool_result。
                    if on_event:
                        try:
                            on_event(f"[tool] {block.get('name')}")
                        except Exception as exc:
                            ctx.extra.setdefault("hook_errors", []).append(f"on_event: {exc}")
                    output = execute_tool(block, registry, ctx, hooks)
                    result.tool_calls += 1
                    outputs.append({"type": "tool_result", "tool_use_id": block["id"],
                                    "content": output.model_content(), "is_error": not output.ok})
                # 一轮中的多个工具结果统一追加，并与每个调用 id 配对。
                messages.append({"role": "user", "content": outputs})
                continue
            result.text = response.text()
            directive = hooks.trigger(STOP, result, ctx)
            if directive is None:
                return result
            if not isinstance(directive, StopDirective):
                raise TypeError("Stop hooks must return None or StopDirective")
            if directive.action == "return":
                result.stop_reason = directive.stop_reason
                if directive.message:
                    result.text = directive.message
                return result
            messages.append({"role": "user", "content": directive.message})
        except Exception as exc:
            result.stop_reason = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            return result
    result.stop_reason = "max_turns"
    result.error = f"reached max_turns={max_turns} before a final response"
    return result
