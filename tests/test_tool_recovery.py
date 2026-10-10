"""Fault injection verifies both budgets without real API calls or sleeps."""

import io
import json
import os
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from agent import Runtime, Settings, load_settings
from agent.events import Hooks, POST_TOOL_USE, PRE_TOOL_USE
from agent.loop import execute_tool
from agent.tools import Tool, ToolRecovery, ToolResult
from agent.tools.news import _retry_after
from agent.tools.recovery import MAX_EVENTS, normalized_path


def response(data):
    return io.BytesIO(json.dumps(data).encode("utf-8"))


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="newsagent-recovery-")
        self.root = Path(self.temporary.name)
        self.settings = Settings(workdir=self.root, memory_enabled=False, tavily_api_key="test-key")
        self.runtime = Runtime.create(self.settings)
        self.delays = []
        self.recovery = ToolRecovery(sleep=self.delays.append, random_value=lambda: 0.5)
        self.runtime.recovery = self.recovery

    def tearDown(self):
        self.runtime.close()
        self.temporary.cleanup()

    def tool(self, name, handler, *, read_only=True, retry_safe=True, schema=None):
        schema = schema if schema is not None else {"type": "object"}
        self.runtime.tools.register(Tool(name, "test", schema, handler, read_only=read_only,
                                         retry_safe=retry_safe), replace=True)

    def key(self, name, args):
        return self.recovery.key(name, args, self.runtime.ctx)

    def test_transient_read_retries_twice_then_succeeds_and_checks_hooks_each_time(self):
        calls, hooks = [], []
        def flaky(args, ctx):
            calls.append(args)
            if len(calls) < 3:
                raise ConnectionError("temporary")
            return ToolResult.success("recovered")
        self.tool("flaky", flaky)
        self.runtime.hooks.register(PRE_TOOL_USE, lambda *args: hooks.append("pre"))
        self.runtime.hooks.register(POST_TOOL_USE, lambda *args: hooks.append("post"))
        result = self.runtime.call_tool("flaky", path="a")
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(result.repair_failures, 0)
        self.assertEqual(self.delays, [0.125, 0.25])
        self.assertEqual(hooks, ["pre", "post"] * 3)
        self.assertEqual([event["auto_retry"] for event in self.recovery.events], [True, True, False])
        self.assertEqual((self.runtime.stats.tool_calls, self.runtime.stats.tool_attempts), (1, 3))

    def test_program_budget_resets_for_each_model_tool_call(self):
        calls = []
        def fail(args, ctx):
            calls.append(True)
            raise TimeoutError("temporary")
        self.tool("flaky", fail)
        first = self.runtime.call_tool("flaky")
        second = self.runtime.call_tool("flaky")
        self.assertEqual((first.attempts, second.attempts), (3, 3))
        self.assertEqual(len(calls), 6)
        self.assertEqual(second.repair_failures, 0)  # TIMEOUT 不在模型修正错误集合中。
        self.assertEqual(self.runtime.stats.tool_attempts, 6)

    def test_read_only_and_retry_safe_are_both_required(self):
        for read_only, retry_safe in [(True, False), (False, True), (False, False)]:
            calls = []
            def fail(args, ctx):
                calls.append(True)
                raise TimeoutError("temporary")
            self.tool("flags", fail, read_only=read_only, retry_safe=retry_safe)
            result = self.runtime.call_tool("flags")
            self.assertEqual(result.attempts, 1)
            self.assertEqual(len(calls), 1)
        self.assertEqual(self.delays, [])

    def test_parameter_failures_have_two_repair_opportunities_and_fourth_is_blocked(self):
        seen = []
        self.runtime.hooks.register(PRE_TOOL_USE, lambda *args: seen.append(True))
        for count, limit in enumerate((21, 22, 23), start=1):
            result = self.runtime.call_tool("search_news", query="AI", limit=limit)
            self.assertEqual(result.error_code, "INVALID_ARGUMENT")
            self.assertEqual((result.repair_failures, result.attempts), (count, 0))
        result = self.runtime.call_tool("search_news", query="AI", limit=5)
        self.assertEqual(result.error_code, "RECOVERY_EXHAUSTED")
        self.assertEqual(result.repair_failures, 3)
        self.assertEqual(seen, [])
        self.assertEqual(self.runtime.stats.tool_attempts, 0)

    def test_only_final_outcome_counts_as_one_repair_failure(self):
        calls = []
        def fail(args, ctx):
            calls.append(True)
            if len(calls) < 3:
                return ToolResult.failure("TRANSIENT_ERROR", "temporary", transient=True, execution_status="failed")
            return ToolResult.failure("NOT_FOUND", "missing", action="inspect", execution_status="failed")
        self.tool("flaky", fail)
        result = self.runtime.call_tool("flaky", path="a")
        self.assertEqual((result.attempts, result.repair_failures), (3, 1))
        self.assertEqual(self.recovery.failures[self.key("flaky", {"path": "a"})], 1)

    def test_success_resets_only_its_own_target_repair_count(self):
        calls = []
        def handler(args, ctx):
            calls.append(args)
            return ToolResult.success("ok") if args.get("fixed") else ToolResult.failure("NOT_FOUND", "missing")
        self.tool("repairable", handler)
        self.runtime.call_tool("repairable", path="a")
        self.runtime.call_tool("repairable", path="b")
        result = self.runtime.call_tool("repairable", path="a", fixed=True)
        self.assertEqual(result.repair_failures, 0)
        self.assertNotIn(self.key("repairable", {"path": "a"}), self.recovery.failures)
        self.assertEqual(self.recovery.failures[self.key("repairable", {"path": "b"})], 1)

    def test_news_keys_normalize_spelling_and_ignore_output_options(self):
        a = self.key("search_news", {"query": " OPEN   Models ", "domains": ["B.com", "a.com.", "a.com"], "limit": 1})
        b = self.key("search_news", {"query": "open models", "time_range": "day", "domains": ["a.com", "b.com"], "limit": 20})
        self.assertEqual(a, b)
        self.assertNotEqual(a, self.key("search_news", {"query": "other topic", "domains": ["a.com", "b.com"]}))
        self.assertNotEqual(a, self.key("search_news", {"query": "open models", "time_range": "week", "domains": ["a.com", "b.com"]}))
        self.assertNotEqual(a, self.key("search_news", {"query": "open models", "domains": ["a.com"]}))
        a = self.key("fetch_article", {"url": "https://EXAMPLE.com:443/news#one", "max_chars": 1})
        b = self.key("fetch_article", {"url": "https://example.com/news#two", "max_chars": 20000, "extract_depth": "advanced"})
        self.assertEqual(a, b)
        self.assertNotEqual(a, self.key("fetch_article", {"url": "https://example.com/news?id=2"}))
        self.assertNotEqual(a, self.key("fetch_article", {"url": "https://example.com/News"}))
        self.assertEqual(self.key("write_file", {"path": "sub/../a"}), self.key("write_file", {"path": str(self.root / "a")}))

    def test_extraction_failure_repair_budget_ignores_depth_and_length(self):
        opener = MagicMock()
        opener.open.side_effect = lambda *args, **kwargs: response({"results": [], "failed_results": [{"error": "test-key"}]})
        with patch("agent.tools.news.build_opener", return_value=opener):
            for depth, chars in [("basic", 100), ("advanced", 100), ("advanced", 200)]:
                result = self.runtime.call_tool("fetch_article", url="https://example.com/news", extract_depth=depth, max_chars=chars)
                self.assertEqual(result.error_code, "EXTRACTION_FAILED")
                self.assertEqual(result.attempts, 1)
            result = self.runtime.call_tool("fetch_article", url="https://example.com/news", max_chars=300)
            self.assertEqual(result.error_code, "RECOVERY_EXHAUSTED")
            self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(self.delays, [])

    def test_unknown_write_requires_complete_read_before_repeating(self):
        calls = []
        path = self.root / "a.txt"
        def write(args, ctx):
            calls.append(True)
            path.write_text("first\nsecond\n", encoding="utf-8")
            if len(calls) == 1:
                raise TimeoutError("response lost after mutation")
            return ToolResult.success("written")
        self.tool("write_file", write, read_only=False, retry_safe=True)
        result = self.runtime.call_tool("write_file", path="a.txt", content="x")
        self.assertEqual((result.error_code, result.attempts, result.execution_status), ("TIMEOUT", 1, "unknown"))
        self.assertEqual(self.runtime.call_tool("write_file", path="./a.txt").error_code, "STATE_CHECK_REQUIRED")
        partial = self.runtime.call_tool("read_file", path="a.txt", offset=1, limit=1)
        self.assertIsNone(partial.inspected_path)
        self.assertEqual(self.runtime.call_tool("write_file", path="a.txt").error_code, "STATE_CHECK_REQUIRED")
        full = self.runtime.call_tool("read_file", path="a.txt")
        self.assertEqual(full.inspected_path, normalized_path("a.txt", self.root))
        self.assertTrue(self.runtime.call_tool("write_file", path="a.txt").ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.delays, [])

    def test_truncated_read_never_clears_uncertain_write(self):
        path = self.root / "large.txt"
        path.write_text("x" * 1000, encoding="utf-8")
        self.recovery.uncertain_files.add(normalized_path("large.txt", self.root))
        self.settings.max_tool_output = 80
        result = self.runtime.call_tool("read_file", path="large.txt", offset=1, limit=10)
        self.assertTrue(result.ok)
        self.assertIsNone(result.inspected_path)
        self.assertEqual(self.runtime.call_tool("write_file", path="large.txt", content="x").error_code, "STATE_CHECK_REQUIRED")

    def test_missing_file_read_resolves_uncertain_state(self):
        self.recovery.uncertain_files.add(normalized_path("missing.txt", self.root))
        result = self.runtime.call_tool("read_file", path="missing.txt")
        self.assertEqual(result.error_code, "NOT_FOUND")
        self.assertIsNotNone(result.inspected_path)
        self.assertTrue(self.runtime.call_tool("write_file", path="missing.txt", content="x").ok)

    def test_shell_failure_is_not_automatically_retried(self):
        calls = []
        def shell(args, ctx):
            calls.append(True)
            return ToolResult.failure("TIMEOUT", "partial side effects", transient=True,
                                      execution_status="unknown", action="inspect_state")
        self.tool("bash", shell, read_only=False, retry_safe=True)
        result = self.runtime.call_tool("bash", command="anything")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(result.model_content())["next_action"], "inspect_state")

    def test_permission_is_rechecked_before_retry_and_can_stop_it(self):
        calls, permissions = [], []
        def handler(args, ctx):
            calls.append(True)
            raise TimeoutError("temporary")
        def guard(block, ctx):
            permissions.append(True)
            return "deny second attempt" if len(permissions) == 2 else None
        self.tool("flaky", handler)
        self.runtime.hooks.register(PRE_TOOL_USE, guard)
        result = self.runtime.call_tool("flaky")
        self.assertEqual(result.error_code, "POLICY_BLOCKED")
        self.assertEqual((len(calls), len(permissions), result.attempts), (1, 2, 1))
        self.assertEqual(self.recovery.executions, 1)

    def test_post_observer_errors_do_not_prevent_recovery_or_other_observers(self):
        calls, seen = [], []
        def handler(args, ctx):
            calls.append(True)
            if len(calls) == 1:
                raise TimeoutError("temporary")
            return ToolResult.success("ok")
        def broken(*args):
            raise RuntimeError("observer failed")
        self.tool("flaky", handler)
        self.runtime.hooks.register(POST_TOOL_USE, broken)
        self.runtime.hooks.register(POST_TOOL_USE, lambda *args: seen.append(True))
        result = self.runtime.call_tool("flaky")
        self.assertTrue(result.ok)
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(self.runtime.ctx.extra["hook_errors"]), 2)

    def test_original_arguments_survive_handler_mutation_during_retry(self):
        seen = []
        def handler(args, ctx):
            seen.append(list(args["nested"]))
            args["nested"].append("mutated")
            if len(seen) < 3:
                raise ConnectionError("temporary")
            return ToolResult.success("ok")
        self.tool("flaky", handler)
        original = {"nested": ["original"]}
        result = execute_tool({"name": "flaky", "input": original}, self.runtime.tools,
                              self.runtime.ctx, self.runtime.hooks, self.recovery)
        self.assertTrue(result.ok)
        self.assertEqual(seen, [["original"]] * 3)
        self.assertEqual(original, {"nested": ["original"]})

    def test_tavily_timeout_and_503_use_program_retries(self):
        for kind in ("timeout", "503"):
            opener = MagicMock()
            error = TimeoutError("test-key") if kind == "timeout" else HTTPError(
                "https://api.tavily.com/search", 503, "test-key", {}, io.BytesIO(b"test-key"))
            opener.open.side_effect = [error, response({"results": []})]
            with patch("agent.tools.news.build_opener", return_value=opener):
                result = self.runtime.call_tool("search_news", query="AI " + kind)
                self.assertTrue(result.ok)
                self.assertEqual(result.attempts, 2)
                self.assertEqual(opener.open.call_count, 2)
                self.assertNotIn("test-key", result.content)

    def test_auth_quota_and_ssl_errors_never_auto_retry(self):
        for status in (401, 403, 432, 433):
            opener = MagicMock()
            opener.open.side_effect = HTTPError("https://api.tavily.com/search", status,
                                               "test-key", {}, io.BytesIO(b"test-key"))
            with patch("agent.tools.news.build_opener", return_value=opener):
                result = self.runtime.call_tool("search_news", query="AI")
                self.assertFalse(result.transient)
                self.assertEqual(opener.open.call_count, 1)
        opener.open.side_effect = URLError(ssl.SSLCertVerificationError("certificate"))
        with patch("agent.tools.news.build_opener", return_value=opener):
            result = self.runtime.call_tool("search_news", query="AI")
            self.assertFalse(result.transient)
        self.assertEqual(self.delays, [])

    def test_rate_limit_retry_after_is_respected_and_long_wait_is_not_cut_short(self):
        opener = MagicMock()
        error = HTTPError("https://api.tavily.com/search", 429, "rate limited", {"Retry-After": "2"}, io.BytesIO())
        opener.open.side_effect = [error, response({"results": []})]
        with patch("agent.tools.news.build_opener", return_value=opener):
            result = self.runtime.call_tool("search_news", query="AI")
            self.assertTrue(result.ok)
            self.assertEqual(self.delays, [2.125])
        opener.open.reset_mock()
        opener.open.side_effect = HTTPError("https://api.tavily.com/search", 429, "rate limited",
                                           {"Retry-After": "60"}, io.BytesIO())
        with patch("agent.tools.news.build_opener", return_value=opener):
            result = self.runtime.call_tool("search_news", query="AI")
            self.assertEqual(result.error_code, "RATE_LIMITED")
            self.assertEqual(result.retry_after, 60)
            self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(self.delays, [2.125])

    def test_retry_after_parser_handles_dates_and_invalid_values(self):
        value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10), usegmt=True)
        self.assertTrue(8 <= _retry_after({"Retry-After": value}) <= 10)
        self.assertEqual(_retry_after({"Retry-After": "-1"}), 0)
        for value in ("bad", "NaN", "inf"):
            self.assertIsNone(_retry_after({"Retry-After": value}))
        self.assertIsNone(_retry_after({}))

    def test_error_output_metadata_survives_clipping(self):
        self.settings.max_tool_output = 20
        self.tool("failure", lambda args, ctx: ToolResult.failure("NOT_FOUND", "x" * 100,
                                                                  action="inspect", execution_status="failed"))
        result = self.runtime.call_tool("failure", path="a")
        data = json.loads(result.model_content())
        self.assertEqual(data["repair_failures"], 1)
        self.assertEqual(data["next_action"], "inspect")
        self.assertEqual(data["execution_status"], "failed")
        self.assertEqual(result.attempts, 1)

    def test_attempt_log_is_bounded_and_contains_hashes_instead_of_arguments(self):
        self.tool("ok", lambda args, ctx: ToolResult.success("long source body"))
        for index in range(MAX_EVENTS + 5):
            self.runtime.call_tool("ok", secret="do-not-log-this", number=index)
        self.assertEqual(len(self.recovery.events), MAX_EVENTS)
        self.assertEqual(self.recovery.dropped_events, 5)
        self.assertEqual(self.recovery.executions, MAX_EVENTS + 5)
        self.assertIs(self.runtime.ctx.extra["tool_attempts"], self.recovery.events)
        serialized = json.dumps(self.recovery.events)
        self.assertNotIn("do-not-log-this", serialized)
        self.assertNotIn("long source body", serialized)
        self.assertEqual(len(self.recovery.events[0]["arguments_sha256"]), 64)

    def test_model_loop_counts_calls_separately_from_program_attempts(self):
        calls = []
        def handler(args, ctx):
            calls.append(True)
            if len(calls) < 3:
                raise TimeoutError("temporary")
            return ToolResult.success("ok")
        traces = []
        with Runtime.create(self.settings, script=[{"tool": "flaky", "input": {}}, "done"], on_event=traces.append) as runtime:
            runtime.tools.register(Tool("flaky", "test", {"type": "object"}, handler, read_only=True, retry_safe=True))
            with patch("agent.tools.recovery.time.sleep"):
                result = runtime.submit("test")
            self.assertTrue(result.ok)
            self.assertEqual((result.turns, result.tool_calls, result.tool_attempts), (2, 1, 3))
            self.assertEqual(runtime.stats.tool_attempts, 3)
            self.assertEqual(len([line for line in traces if line.startswith("[retry]")]), 2)
            self.assertEqual(len(runtime.messages[2]["content"]), 1)

    def test_repair_budget_is_shared_across_rounds_and_resets_next_request(self):
        failing = {"tool": "search_news", "input": {"query": "AI", "limit": 21}}
        script = [failing, failing, failing, failing, "done", failing, "again"]
        with Runtime.create(self.settings, script=script) as runtime:
            result = runtime.submit("first")
            outputs = [json.loads(message["content"][0]["content"]) for message in runtime.messages
                       if isinstance(message["content"], list) and message["content"][0]["type"] == "tool_result"]
            self.assertTrue(result.ok)
            self.assertEqual([output["error_code"] for output in outputs], ["INVALID_ARGUMENT"] * 3 + ["RECOVERY_EXHAUSTED"])
            self.assertEqual(result.tool_attempts, 0)
            self.assertTrue(runtime.submit("second").ok)
            output = json.loads(runtime.messages[-2]["content"][0]["content"])
            self.assertEqual((output["error_code"], output["repair_failures"]), ("INVALID_ARGUMENT", 1))

    def test_reset_clears_direct_call_scope_and_runtimes_are_isolated(self):
        for _ in range(3):
            self.runtime.call_tool("search_news", query="AI", limit=21)
        self.assertEqual(self.runtime.call_tool("search_news", query="AI").error_code, "RECOVERY_EXHAUSTED")
        with Runtime.create(self.settings) as other:
            result = other.call_tool("search_news", query="AI", limit=21)
            self.assertEqual(result.repair_failures, 1)
        self.runtime.reset()
        result = self.runtime.call_tool("search_news", query="AI", limit=21)
        self.assertEqual(result.repair_failures, 1)

    def test_zero_budgets_and_env_configuration(self):
        self.runtime.recovery = ToolRecovery(max_retries=0, max_repairs=0, sleep=self.delays.append)
        self.tool("timeout", lambda args, ctx: ToolResult.failure("TIMEOUT", "temporary", transient=True))
        result = self.runtime.call_tool("timeout")
        self.assertEqual(result.attempts, 1)
        self.runtime.call_tool("search_news", query="AI", limit=21)
        self.assertEqual(self.runtime.call_tool("search_news", query="AI").error_code, "RECOVERY_EXHAUSTED")
        env = self.root / "settings.env"
        env.write_text("AGENT_MAX_TOOL_RETRIES=0\nAGENT_MAX_TOOL_REPAIRS=1\n", encoding="utf-8")
        with patch.dict(os.environ, {"AGENT_MAX_TOOL_RETRIES": "3"}, clear=True):
            settings = load_settings(env, workdir=self.root)
            self.assertEqual((settings.max_tool_retries, settings.max_tool_repairs), (3, 1))
            self.assertEqual(load_settings(env, workdir=self.root, max_tool_retries=0).max_tool_retries, 0)
        for name, value in [("max_tool_retries", -1), ("max_tool_retries", 6), ("max_tool_retries", True),
                            ("max_tool_repairs", 11), ("max_tool_repairs", 1.5)]:
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                Settings(workdir=self.root, **{name: value})

    def test_permanent_errors_cannot_retry_even_if_incorrectly_marked_transient(self):
        for code in ("PERMISSION_DENIED", "INVALID_ARGUMENT", "AUTH_ERROR", "QUOTA_EXCEEDED"):
            self.tool("permanent", lambda args, ctx: ToolResult.failure(code, "permanent", transient=True))
            result = self.runtime.call_tool("permanent")
            self.assertEqual(result.attempts, 1)
        self.assertEqual(self.delays, [])


if __name__ == "__main__":
    unittest.main()
