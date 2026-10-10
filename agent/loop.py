"""用户消息 -> 模型 -> 工具 -> 工具结果 -> 模型，直到最终回复。"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, replace
from typing import Callable

from .events import Hooks, POST_TOOL_USE, PRE_TOOL_USE, STOP, StopDirective
from .llm import LLMClient, normalize_response
from .tools.registry import ToolContext, ToolRegistry
from .tools.result import ToolResult
from .tools.recovery import ToolRecovery


# 与 HarnessAgent 一致：连续不完整响应最多额外请求两次；仍受 max_turns 限制。
MAX_INCOMPLETE_RETRIES = 2
FULL_ANSWER_INSTRUCTION = (
    "Your response was cut off. Use the draft and earlier tool results to finish the task. "
    "Return one complete, self-contained answer from the beginning, including all necessary "
    "sections and source links; do not return only the remaining tail. Keep it concise enough "
    "to fit the output limit. Do not repeat completed tool calls or invent missing facts."
)


@dataclass
class LoopResult:
    text: str = ""
    turns: int = 0
    tool_calls: int = 0
    stop_reason: str = "final"  # final | max_turns | error | blocked | incomplete
    error: str = ""
    tool_attempts: int = 0  # handler 执行次数，包括程序内部重试。

    @property
    def ok(self) -> bool:
        return self.stop_reason == "final"


def execute_tool(block: dict, registry: ToolRegistry, ctx: ToolContext, hooks: Hooks,
                 recovery: ToolRecovery | None = None,
                 on_event: Callable[[str], None] | None = None) -> ToolResult:
    recovery = recovery if recovery is not None else ToolRecovery(
        max_retries=ctx.settings.max_tool_retries, max_repairs=ctx.settings.max_tool_repairs)
    name, args = block.get("name"), copy.deepcopy(block.get("input"))
    ctx.extra["tool_attempts"] = recovery.events
    key = recovery.key(name, args, ctx)
    # 限制工具输出长度
    def clip(result: ToolResult) -> ToolResult:
        limit = ctx.settings.max_tool_output
        if len(result.content) > limit:
            # 被截断的 read_file 不能作为“已经完整检查”的证据。
            return replace(result, content=result.content[:limit] + "\n[output truncated]", inspected_path=None)
        return result
    # 通知所有PostToolUse hook
    def notify(result: ToolResult) -> None:
        # 观察者异常不改写结果；每次真实尝试都通知所有 PostToolUse。
        for callback in hooks.callbacks(POST_TOOL_USE):
            try:
                callback(copy.deepcopy(block), result, ctx)
            except Exception as exc:
                ctx.extra.setdefault("hook_errors", []).append(f"PostToolUse: {type(exc).__name__}: {exc}")
    # 发送过程消息
    def emit(message: str) -> None:
        if on_event:
            try:
                on_event(message)
            except Exception as exc:
                ctx.extra.setdefault("hook_errors", []).append(f"on_event: {exc}")
    # 统一处理被拦截的调用   这个函数处理：恢复规则拦截 参数校验失败 PreToolUse报错 PreToolUse拒绝执行
    def rejected(result: ToolResult, phase: str, attempts: int = 0) -> ToolResult:
        result = recovery.observe(key, clip(replace(result, attempts=attempts)))
        recovery.record(name, args, result, phase=phase, attempt=0)
        notify(result)
        if result.error_code in {"STATE_CHECK_REQUIRED", "RECOVERY_EXHAUSTED"}:
            emit(f"[recovery] {name}: {result.error_code}")
        return result
    # 先检查恢复规则，再校验参数，检查大模型修正次数
    exhausted = recovery.exhausted(key)
    # 已经被恢复规则拦住，就不要继续校验参数。否则调用注册表校验参数合法性
    invalid = exhausted if exhausted is not None else registry.validate(name, args)
    if invalid is not None:
        # 统一处理失败
        return rejected(invalid, "recovery" if exhausted is not None else "validation")
    # 进入自动重试循环
    # 同一份参数重复执行；每次重试都重新经过 PreToolUse。
    for attempt in range(1, recovery.max_retries + 2):
        try:
            blocked = hooks.trigger(PRE_TOOL_USE, copy.deepcopy(block), ctx)
        except Exception as exc:
            return rejected(ToolResult.failure("HOOK_ERROR", f"PreToolUse: {type(exc).__name__}: {exc}"),
                            "permission", attempt - 1)
        if blocked is not None:
            return rejected(ToolResult.failure("POLICY_BLOCKED", str(blocked.content)
                                               if isinstance(blocked, ToolResult) else str(blocked)),
                            "permission", attempt - 1)
        started = time.monotonic()
        output = clip(replace(registry.dispatch(name, args, ctx), attempts=attempt))
        elapsed = time.monotonic() - started
        retry = recovery.can_retry(registry.get(name), output, attempt)
        if not retry:
            output = recovery.observe(key, output)
        notify(output)
        recovery.record(name, args, output, phase="execution", attempt=attempt, elapsed=elapsed, retry=retry)
        if not retry:
            return output
        emit(f"[retry] {name}: {output.error_code}; next attempt {attempt + 1}/{recovery.max_retries + 1}")
        recovery.wait(attempt, output)
    raise AssertionError("tool retry budget was not enforced")


def run_loop(*, llm: LLMClient, registry: ToolRegistry, messages: list[dict],
             system: str | Callable[[], str], ctx: ToolContext, hooks: Hooks | None = None,
             max_turns: int = 30, on_event: Callable[[str], None] | None = None,
             recovery: ToolRecovery | None = None) -> LoopResult:
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0:
        raise ValueError("max_turns must be a positive integer")
    hooks = hooks if hooks is not None else Hooks()
    recovery = recovery if recovery is not None else ToolRecovery(
        max_retries=ctx.settings.max_tool_retries, max_repairs=ctx.settings.max_tool_repairs)
    ctx.extra["tool_attempts"] = recovery.events
    initial_executions = recovery.executions
    result = LoopResult()
    incomplete_retries = 0
    recovery_note = ""
    for turn in range(1, max_turns + 1):
        result.turns = turn
        try:
            if on_event:
                on_event(f"[model] round {turn}")
            system_prompt = system() if callable(system) else system
            if recovery_note:
                system_prompt += "\n\n" + recovery_note
            response = normalize_response(llm.create(
                system=system_prompt,
                messages=copy.deepcopy(messages), tools=registry.definitions(),
                max_tokens=ctx.settings.max_tokens))
            calls = [block for block in response.content if block.get("type") == "tool_use"]
            final_text = response.text()
            # 拒绝和 pause_turn 不属于这次迁移的恢复策略，也不执行其中的工具。
            if response.stop_reason == "refusal":
                result.text = final_text
                result.stop_reason = "blocked"
                return result
            if response.stop_reason == "pause_turn":
                result.text = final_text
                result.stop_reason = "incomplete"
                result.error = f"model response was incomplete: {response.stop_reason}"
                return result
            truncated = response.stop_reason == "max_tokens"
            if truncated or (not calls and not final_text.strip()):
                detail = "max_tokens" if truncated else "no answer text or tool calls"
                if incomplete_retries >= MAX_INCOMPLETE_RETRIES:
                    result.text = final_text if not calls else ""
                    result.stop_reason = "incomplete"
                    result.error = (f"model response was incomplete: {detail}; automatic recovery exhausted "
                                    f"after {MAX_INCOMPLETE_RETRIES} retries. "
                                    "Increase AGENT_MAX_TOKENS or request smaller steps if max_tokens is reported.")
                    return result
                incomplete_retries += 1
                if calls:
                    # 即使部分调用看似完整或 ID 无效，也丢弃整个截断批次，不留悬空 tool_use。
                    recovery_note = (
                        "[agent recovery] The previous response reached max_tokens while generating tool calls. "
                        "None of that response's tool calls were executed; earlier completed tool results remain valid. "
                        "Regenerate the next step as ONE small, complete tool call. Keep reasoning short, "
                        "split large writes into smaller steps, and do not assume discarded calls succeeded. "
                        "The eventual final answer must be complete and self-contained."
                    )
                else:
                    recovery_note = (
                        "[agent recovery] The previous response was incomplete. Continue the user's task "
                        "with a concrete tool call or a concise, complete answer. Keep reasoning and each "
                        "step short; do not claim completion without evidence."
                    )
                    if truncated and final_text.strip():
                        # 保留纯文本草稿供重写；截断的 thinking/signature 不发回服务端。
                        messages.append({"role": "assistant", "content": [{"type": "text", "text": final_text}]})
                        messages.append({"role": "user", "content": FULL_ANSWER_INSTRUCTION})
                        recovery_note += " " + FULL_ANSWER_INSTRUCTION
                    # 空响应、只有思考块的响应不追加到历史，也不触发 Stop。
                if on_event:
                    try:
                        on_event(f"[model recovery] {detail}; retry {incomplete_retries}/{MAX_INCOMPLETE_RETRIES}")
                    except Exception as exc:
                        ctx.extra.setdefault("hook_errors", []).append(f"on_event: {exc}")
                continue
            ids = [block.get("id") for block in calls]
            if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
                raise ValueError("tool_use ids must be nonempty and unique within a response")
            if response.stop_reason == "tool_use" and not calls:
                raise ValueError("tool_use stop_reason without tool_use blocks")
            incomplete_retries = 0
            recovery_note = ""
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
                    output = execute_tool(block, registry, ctx, hooks, recovery, on_event)
                    result.tool_calls += 1
                    result.tool_attempts = recovery.executions - initial_executions
                    outputs.append({"type": "tool_result", "tool_use_id": block["id"],
                                    "content": output.model_content(), "is_error": not output.ok})
                # 一轮中的多个工具结果统一追加，并与每个调用 id 配对。
                messages.append({"role": "user", "content": outputs})
                continue
            result.text = final_text
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
            result.tool_attempts = recovery.executions - initial_executions
            result.stop_reason = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            return result
    result.stop_reason = "max_turns"
    result.error = f"reached max_turns={max_turns} before a final response"
    return result
