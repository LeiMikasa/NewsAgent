"""Adapted from D:/HarnessAgent/agent/tools/recovery.py (MIT).

Each recovery scope owns model repair counts, uncertain file states and a
bounded attempt log. Automatic retries have a fresh budget for EACH tool call.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .result import ToolResult

# 可以增加失败计数的错误
                # 工具参数不符合要求    # 调用了不存在的工具 # 文件或目标不存在 # 执行条件不满足
REPAIR_CODES = {"INVALID_ARGUMENT", "UNKNOWN_TOOL", "NOT_FOUND", "PRECONDITION_FAILED",
                # 新闻API拒绝请求参数  # 文章提取失败          # 没有得到有效正文
                "INVALID_REQUEST", "EXTRACTION_FAILED", "EMPTY_CONTENT"}
# 禁止程序自动重试的错误
NEVER_RETRY_CODES = {"INVALID_ARGUMENT", "INVALID_REQUEST", "PERMISSION_DENIED", "POLICY_BLOCKED",
                     "HOOK_ERROR", "AUTH_ERROR", "MISSING_API_KEY", "QUOTA_EXCEEDED",
                     "STATE_CHECK_REQUIRED", "RECOVERY_EXHAUSTED"}
MAX_EVENTS = 200        # 保留最近200条恢复日志
MAX_RETRY_DELAY = 30.0  # 单次程序重试等待最多30秒

# 给参数生成指纹 ，为新闻查询，文章URL生成计数目标   在执行日志中记录参数指纹，避免保存完整参数
def arguments_hash(args) -> str:
    try:
        serialized = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        serialized = repr(args)
    return hashlib.sha256(serialized.encode("utf-8", errors="replace")).hexdigest()

# 统一文件路径
def normalized_path(path: str, workdir: Path) -> str:
    candidate = Path(path).expanduser()
    return os.path.normcase(str((candidate if candidate.is_absolute() else Path(workdir) / candidate).resolve()))

# 统一文章URL
def normalized_url(url: str) -> str:
    """Only normalize equivalent spelling; preserve query and path case."""
    try:
        parts = urlsplit(url.strip())
        if not parts.hostname or parts.username is not None or parts.password is not None:
            return url.strip()
        host = parts.hostname.lower().rstrip(".")
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        if port is not None and (parts.scheme, port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, ""))
    except ValueError:
        return url.strip()


class ToolRecovery:
    def __init__(self, *, max_retries: int = 2, max_repairs: int = 2, sleep=None, random_value=None):
        for name, value, maximum in (("max_retries", max_retries, 5), ("max_repairs", max_repairs, 10)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 0 and {maximum}")
        self.max_retries, self.max_repairs = max_retries, max_repairs
        self.sleep = sleep if sleep is not None else time.sleep
        self.random_value = random_value if random_value is not None else random.random
        self.failures: dict[tuple, int] = {}  # 每个工具，目标累计的可修正失败次数
        self.uncertain_files: set[str] = set() #上一次写入结果不确定的文件目标
        self.events: list[dict] = []     # 保存执行和拦截日志，最多保留200条
        self.dropped_events = 0    # 统计有多少条旧日志因为容量限制被丢弃
        self.executions = 0        # 实际执行工具handler的总次数
    # 给工具调用确定一个计数身份
    @staticmethod
    def key(name, args, ctx) -> tuple:
        """Stable repair target; output-size options never reset the budget."""
        target = "<default>"
        if isinstance(args, dict):
            if name == "search_news":
                query = args.get("query", "")
                query = " ".join(query.casefold().split()) if isinstance(query, str) else query
                domains = args.get("domains", [])
                if isinstance(domains, list) and all(isinstance(domain, str) for domain in domains):
                    domains = sorted({domain.lower().rstrip(".") for domain in domains})
                target = arguments_hash([query, args.get("time_range", "day"), domains])
            elif name == "fetch_article":
                url = args.get("url", "")
                target = arguments_hash(normalized_url(url) if isinstance(url, str) else url)
            else:
                for field in ("path", "dir", "task_id", "id", "request_id", "service", "name", "to"):
                    if field in args:
                        target = str(args[field])
                        if field in {"path", "dir"} and isinstance(args[field], str):
                            try:
                                target = normalized_path(args[field], ctx.workdir)
                            except (OSError, RuntimeError, ValueError):
                                pass
                        break
        return (str(ctx.workdir), str(name), target)
    # 执行前判断是否应该拦住   返回None,运行继续检查和执行   返回ToolResult拦截，并说明原因
    def exhausted(self, key: tuple) -> ToolResult | None:
        if key[1] in {"write_file", "edit_file"} and key[2] in self.uncertain_files:
            return ToolResult.failure("STATE_CHECK_REQUIRED",
                "Previous file mutation has an unknown outcome. Read the complete file before another mutation.",
                action="inspect_state")
        if self.failures.get(key, 0) >= 1 + self.max_repairs:
            return ToolResult.failure("RECOVERY_EXHAUSTED",
                "Repair budget exhausted for this tool/target in this request; report the blocker or choose another target.")
        return None
    # 根据结果更新状态 这次调用结束了，根据结果修改我的记录
    def observe(self, key: tuple, result: ToolResult) -> ToolResult:
        """Observe only a call's FINAL outcome, never each internal retry."""
        # 是文件修改工具  结果失败  执行状态不确定
        if key[1] in {"write_file", "edit_file"} and not result.ok and result.execution_status == "unknown":
            self.uncertain_files.add(key[2])
        # 判断这是不是一个足以确认文件状态的读取结果
        # 工具是read_file  工具提供了inspected_path
        if key[1] == "read_file" and result.inspected_path is not None and (result.ok or result.error_code == "NOT_FOUND"):
            self.uncertain_files.discard(result.inspected_path)
        if result.ok:
            self.failures.pop(key, None)
        elif result.error_code in REPAIR_CODES:
            self.failures[key] = self.failures.get(key, 0) + 1
        return replace(result, repair_failures=self.failures.get(key, 0))

    # 当前结果能不能由程序自动重试
    def can_retry(self, tool, result: ToolResult, attempt: int) -> bool:
        hint = result.retry_after # 去服务端建议的等待秒数
        if (isinstance(hint, (int, float)) and not isinstance(hint, bool)
                and math.isfinite(hint) and hint > MAX_RETRY_DELAY):
            return False  # 不为了缩短等待而提前违反服务端 Retry-After。
        return bool(tool and tool.read_only and tool.retry_safe and not result.ok and result.transient
                    and attempt <= self.max_retries and result.error_code not in NEVER_RETRY_CODES)

    # 记录一次尝试，但不改变恢复状态
    def record(self, name, args, result, *, phase, attempt, elapsed=0.0, retry=False) -> None:
        if phase == "execution":
            self.executions += 1
        if len(self.events) >= MAX_EVENTS:
            del self.events[0]
            self.dropped_events += 1
        self.events.append({"tool": str(name), "arguments_sha256": arguments_hash(args),
                            "phase": phase, "attempt": attempt, "ok": result.ok,
                            "error_code": result.error_code, "execution_status": result.execution_status,
                            "repair_failures": result.repair_failures,
                            "elapsed_seconds": round(elapsed, 6), "auto_retry": retry})

    def wait(self, attempt: int, result: ToolResult) -> float:
        hint = result.retry_after
        if isinstance(hint, bool) or not isinstance(hint, (int, float)) or not math.isfinite(hint):
            hint = 0.0
        # HarnessAgent's exponential full jitter, with bounded Retry-After support.
        jitter = self.random_value() * min(2.0, 0.25 * (2 ** (attempt - 1)))
        delay = min(MAX_RETRY_DELAY, max(0.0, hint) + jitter)
        self.sleep(delay)
        return delay
