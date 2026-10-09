import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import Runtime, Settings, load_settings
from agent.demo import DEMO_PROMPT, DEMO_SCRIPT
from agent.events import (Hooks, POST_TOOL_USE, PRE_TOOL_USE, SESSION_END,
                          SESSION_START, STOP, USER_PROMPT_SUBMIT, StopDirective)
from agent.llm import LLMResponse
from agent.llm import AnthropicLLM
from agent.tools import Tool, ToolResult


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="newsagent-test-")
        self.root = Path(self.temporary.name)
        self.settings = Settings(workdir=self.root)
        self.runtime = Runtime.create(self.settings)

    def tearDown(self):
        self.runtime.close()
        self.temporary.cleanup()

    def test_all_five_tools_in_model_loop(self):
        with Runtime.create(self.settings, script=DEMO_SCRIPT) as runtime:
            result = runtime.submit(DEMO_PROMPT)
            self.assertTrue(result.ok, result.error)
            self.assertEqual((result.turns, result.tool_calls), (3, 5))
            self.assertEqual(runtime.stats.tool_calls, 5)
            self.assertEqual((self.root / ".agent/demo/news.txt").read_text(),
                             "AI news demo: agent loop is ready.\n")
            self.assertEqual(runtime.llm.calls[1]["messages"][-1]["content"][0]["tool_use_id"], "mock_1")
            results = runtime.llm.calls[2]["messages"][-1]["content"]
            self.assertEqual(len(results), 4)
            self.assertTrue(all(not item["is_error"] for item in results))
            self.assertIn("newsagent-shell-ok", results[-1]["content"])
            self.assertEqual(json.loads(results[2]["content"])["matches"][0]["line"], 1)

    def test_write_read_list_and_unicode(self):
        content = "今日 AI 新闻\n第二行\n"
        result = self.runtime.call_tool("write_file", path="新闻/日报.txt", content=content)
        self.assertTrue(result.ok)
        self.assertEqual(json.loads(result.content)["bytes_written"], len(content.encode("utf-8")))
        self.assertEqual(self.runtime.call_tool("read_file", path="新闻/日报.txt").content, content)
        listing = json.loads(self.runtime.call_tool("list_dir", path="新闻").content)
        self.assertEqual(listing["entries"][0]["name"], "日报.txt")
        window = json.loads(self.runtime.call_tool("read_file", path="新闻/日报.txt", offset=2, limit=1).content)
        self.assertEqual(window["content"], "第二行\n")
        self.assertEqual(window["total_lines"], 2)

    def test_overwrite_protection_and_empty_write(self):
        self.runtime.call_tool("write_file", path="a.txt", content="original")
        result = self.runtime.call_tool("write_file", path="a.txt", content="new", overwrite=False)
        self.assertEqual(result.error_code, "ALREADY_EXISTS")
        self.assertEqual((self.root / "a.txt").read_text(), "original")
        self.assertTrue(self.runtime.call_tool("write_file", path="a.txt", content="").ok)
        self.assertEqual((self.root / "a.txt").read_text(), "")
        self.assertEqual(list(self.root.glob(".newsagent-*")), [])

    def test_invalid_arguments_have_no_side_effects(self):
        cases = [
            ("write_file", {"path": "a.txt"}),
            ("write_file", {"path": "a.txt", "content": 123}),
            ("write_file", {"path": "a.txt", "content": "ok", "typo": True}),
            ("write_file", {"path": "  ", "content": "ok"}),
            ("read_file", {"path": "a.txt", "offset": True}),
            ("bash", {"command": "echo ok", "timeout": 0}),
            ("bash", {"command": "echo ok", "timeout": float("nan")}),
            ("search_content", {"keyword": "", "dir": "."}),
        ]
        for name, args in cases:
            with self.subTest(name=name, args=args):
                self.assertEqual(self.runtime.call_tool(name, **args).error_code, "INVALID_ARGUMENT")
        self.assertFalse((self.root / "a.txt").exists())
        self.assertEqual(self.runtime.call_tool("missing").error_code, "UNKNOWN_TOOL")

    def test_workspace_escape_rejected(self):
        cases = [("read_file", {"path": "../outside.txt"}),
                 ("write_file", {"path": "../outside.txt", "content": "x"}),
                 ("list_dir", {"path": ".."}),
                 ("search_content", {"keyword": "x", "dir": ".."})]
        for name, args in cases:
            with self.subTest(name=name):
                self.assertEqual(self.runtime.call_tool(name, **args).error_code, "PERMISSION_DENIED")

    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory(prefix="newsagent-outside-") as outside:
            target = Path(outside) / "private.txt"
            target.write_text("private", encoding="utf-8")
            link = self.root / "link.txt"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("host does not permit symlink creation")
            self.assertEqual(self.runtime.call_tool("read_file", path="link.txt").error_code, "PERMISSION_DENIED")
            self.assertEqual(self.runtime.call_tool("write_file", path="link.txt", content="x").error_code, "PERMISSION_DENIED")
            result = json.loads(self.runtime.call_tool("search_content", keyword="private", dir=".").content)
            self.assertEqual(result["matches"], [])
            self.assertEqual(target.read_text(), "private")

    def test_missing_and_directory_errors(self):
        self.assertEqual(self.runtime.call_tool("read_file", path="absent").error_code, "NOT_FOUND")
        self.assertEqual(self.runtime.call_tool("read_file", path=".").error_code, "PRECONDITION_FAILED")
        self.runtime.call_tool("write_file", path="file", content="x")
        self.assertEqual(self.runtime.call_tool("list_dir", path="file").error_code, "PRECONDITION_FAILED")

    def test_search_literal_case_and_exclusions(self):
        (self.root / "a.txt").write_text("AI [x]\nai [x]\n", encoding="utf-8")
        (self.root / "binary.dat").write_bytes(b"\x00AI")
        (self.root / ".git").mkdir()
        (self.root / ".git/hidden").write_text("AI")
        result = json.loads(self.runtime.call_tool("search_content", keyword="[x]", dir=".").content)
        self.assertEqual([item["line"] for item in result["matches"]], [1, 2])
        self.assertGreaterEqual(result["skipped_entries"], 2)
        sensitive = json.loads(self.runtime.call_tool("search_content", keyword="AI", dir=".", case_sensitive=True).content)
        self.assertEqual(len(sensitive["matches"]), 1)
        bounded = json.loads(self.runtime.call_tool("search_content", keyword="ai", dir=".", max_results=1).content)
        self.assertTrue(bounded["truncated"])

    def test_file_size_binary_and_output_limits(self):
        settings = Settings(workdir=self.root, max_file_bytes=100, max_tool_output=30)
        (self.root / "large").write_bytes(b"x" * 101)
        (self.root / "binary").write_bytes(b"\x00")
        (self.root / "text").write_text("x" * 80)
        with Runtime.create(settings) as runtime:
            self.assertEqual(runtime.call_tool("read_file", path="large").error_code, "FILE_TOO_LARGE")
            self.assertEqual(runtime.call_tool("read_file", path="binary").error_code, "NOT_TEXT")
            self.assertIn("[output truncated]", runtime.call_tool("read_file", path="text").content)

    def test_list_and_search_scan_limits(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.root / name).write_text("AI")
        listing = json.loads(self.runtime.call_tool("list_dir", path=".", limit=1).content)
        self.assertTrue(listing["truncated"])
        self.assertEqual(len(listing["entries"]), 1)
        search = json.loads(self.runtime.call_tool("search_content", keyword="AI", dir=".", max_files=1).content)
        self.assertTrue(search["truncated"])
        self.assertEqual(search["scanned_files"], 1)

    def test_hook_veto_and_observation(self):
        events = []
        self.runtime.hooks.register(PRE_TOOL_USE, lambda block, ctx: "blocked by test")
        self.runtime.hooks.register(POST_TOOL_USE, lambda block, result, ctx: events.append(result.error_code))
        result = self.runtime.call_tool("write_file", path="blocked.txt", content="x")
        self.assertEqual(result.error_code, "POLICY_BLOCKED")
        self.assertFalse((self.root / "blocked.txt").exists())
        self.assertEqual(events, ["POLICY_BLOCKED"])

    def test_pre_hook_failure_fails_closed(self):
        def broken(*args):
            raise RuntimeError("hook broke")
        self.runtime.hooks.register(PRE_TOOL_USE, broken)
        result = self.runtime.call_tool("write_file", path="blocked.txt", content="x")
        self.assertEqual(result.error_code, "HOOK_ERROR")
        self.assertFalse((self.root / "blocked.txt").exists())

    def test_post_hook_failure_keeps_result_and_other_observers(self):
        seen = []
        def broken(*args):
            raise RuntimeError("observer broke")
        self.runtime.hooks.register(POST_TOOL_USE, broken)
        self.runtime.hooks.register(POST_TOOL_USE, lambda *args: seen.append(True))
        result = self.runtime.call_tool("write_file", path="a.txt", content="x")
        self.assertTrue(result.ok)
        self.assertEqual((self.root / "a.txt").read_text(), "x")
        self.assertEqual(seen, [True])
        self.assertIn("observer broke", self.runtime.ctx.extra["hook_errors"][0])

    def test_tool_error_returns_to_model(self):
        def responder(index, messages, tools):
            if index == 0:
                return {"tool": "read_file", "input": {"path": "missing"}}
            result = messages[-1]["content"][0]
            self.assertTrue(result["is_error"])
            self.assertEqual(json.loads(result["content"])["error_code"], "NOT_FOUND")
            return "文件不存在。"
        with Runtime.create(self.settings, responder=responder) as runtime:
            result = runtime.submit("读取文件")
            self.assertTrue(result.ok)
            self.assertEqual(result.text, "文件不存在。")

    def test_stop_hook_continue_and_budget(self):
        hooks = Hooks()
        def stop(result, ctx):
            if result.turns == 1:
                return StopDirective("continue", "再确认一次。")
        hooks.register(STOP, stop)
        with Runtime.create(self.settings, script=["初步完成", "已确认"], hooks=hooks) as runtime:
            result = runtime.submit("测试")
            self.assertEqual((result.text, result.turns), ("已确认", 2))
            self.assertEqual(runtime.llm.calls[1]["messages"][-1]["content"], "再确认一次。")
        settings = Settings(workdir=self.root, max_turns=2)
        hooks = Hooks()
        hooks.register(STOP, lambda *_: StopDirective("continue", "继续"))
        with Runtime.create(settings, script=["a", "b"], hooks=hooks) as runtime:
            self.assertEqual(runtime.submit("test").stop_reason, "max_turns")

    def test_stop_hook_return(self):
        self.runtime.hooks.register(STOP, lambda *_: StopDirective("return", "已暂停", "blocked"))
        result = self.runtime.submit("测试")
        self.assertEqual((result.stop_reason, result.text), ("blocked", "已暂停"))

    def test_budget_keeps_tool_result_pairing(self):
        settings = Settings(workdir=self.root, max_turns=1)
        with Runtime.create(settings, script=[{"tool": "list_dir", "input": {"path": "."}}, "done"]) as runtime:
            result = runtime.submit("test")
            self.assertEqual(result.stop_reason, "max_turns")
            self.assertEqual(runtime.messages[-1]["content"][0]["type"], "tool_result")
            self.assertTrue(runtime.submit("继续").ok)

    def test_incomplete_response_has_no_tool_side_effects(self):
        response = LLMResponse([{"type": "tool_use", "id": "partial", "name": "write_file",
                                 "input": {"path": "bad.txt", "content": "partial"}}], "max_tokens")
        with Runtime.create(self.settings, script=[response]) as runtime:
            self.assertEqual(runtime.submit("test").stop_reason, "incomplete")
            self.assertFalse((self.root / "bad.txt").exists())
            self.assertEqual(len(runtime.messages), 1)

    def test_duplicate_call_ids_have_no_side_effects(self):
        response = LLMResponse([{"type": "tool_use", "id": "dup", "name": "write_file",
                                 "input": {"path": "bad.txt", "content": "x"}}] * 2, "tool_use")
        with Runtime.create(self.settings, script=[response]) as runtime:
            self.assertEqual(runtime.submit("test").stop_reason, "error")
            self.assertFalse((self.root / "bad.txt").exists())

    def test_model_error_and_mock_exhaustion(self):
        def broken(*args):
            raise ConnectionError("offline")
        with Runtime.create(self.settings, responder=broken) as runtime:
            result = runtime.submit("test")
            self.assertFalse(result.ok)
            self.assertIn("ConnectionError", result.error)
        with Runtime.create(self.settings, script=[]) as runtime:
            self.assertEqual(runtime.submit("test").stop_reason, "error")

    def test_lifecycle_reset_and_session_isolation(self):
        events = []
        hooks = self.runtime.hooks
        hooks.register(SESSION_START, lambda ctx: events.append("start"))
        hooks.register(SESSION_END, lambda ctx: events.append("end"))
        self.runtime.submit("one")
        self.runtime.submit("two")
        self.assertEqual(len(self.runtime.messages), 4)
        self.assertEqual(self.runtime.stats.requests, 2)
        hooks.register(USER_PROMPT_SUBMIT, lambda *_: "blocked")
        self.assertEqual(self.runtime.submit("three").stop_reason, "blocked")
        self.assertEqual(len(self.runtime.messages), 4)
        self.runtime.reset()
        self.assertEqual(self.runtime.messages, [])
        other = Runtime.create(self.settings)
        self.assertEqual(other.hooks.callbacks(USER_PROMPT_SUBMIT), [])
        other.close()
        self.runtime.close()
        self.runtime.close()
        self.assertEqual(events, ["start", "end"])
        with self.assertRaises(RuntimeError):
            self.runtime.call_tool("list_dir", path=".")

    def test_dynamic_tool_registration(self):
        tool = Tool("ping", "ping", {"type": "object", "additionalProperties": False},
                    lambda args, ctx: ToolResult.success("pong"), read_only=True)
        with Runtime.create(self.settings, script=[{"tool": "ping"}, "done"]) as runtime:
            runtime.tools.register(tool)
            self.assertTrue(runtime.submit("ping").ok)
            self.assertIn("ping", [item["name"] for item in runtime.llm.calls[0]["tools"]])
            self.assertEqual(runtime.messages[2]["content"][0]["content"], "pong")

    def test_bash_disabled_and_nonzero_exit(self):
        settings = Settings(workdir=self.root, allow_bash=False)
        with Runtime.create(settings) as runtime:
            self.assertEqual(runtime.call_tool("bash", command="echo blocked").error_code, "POLICY_BLOCKED")
        if not shutil.which("bash"):
            self.skipTest("bash not installed")
        result = self.runtime.call_tool("bash", command="echo failure >&2; exit 7")
        self.assertEqual(result.error_code, "COMMAND_FAILED")
        value = json.loads(result.content)
        self.assertEqual(value["exit_code"], 7)
        self.assertIn("failure", value["stderr"])

    def test_bash_timeout_and_output_limit(self):
        if not shutil.which("bash"):
            self.skipTest("bash not installed")
        started = time.monotonic()
        result = self.runtime.call_tool("bash", command="sleep 8", timeout=0.2)
        self.assertEqual(result.error_code, "TIMEOUT")
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(json.loads(result.content)["timed_out"])
        bounded = self.runtime.call_tool("bash", command="printf '%050000d' 0")
        self.assertTrue(bounded.ok)
        self.assertTrue(json.loads(bounded.content)["truncated"])

    def test_configuration_precedence(self):
        env_file = self.root / "settings.env"
        env_file.write_text('AGENT_PROVIDER=mock\nAGENT_MAX_TURNS=7\nAGENT_ALLOW_BASH=false\n', encoding="utf-8")
        with patch.dict(os.environ, {"AGENT_MAX_TURNS": "9"}, clear=True):
            settings = load_settings(env_file, workdir=self.root, max_turns=3)
            self.assertEqual(settings.max_turns, 3)
            self.assertFalse(settings.allow_bash)
            self.assertEqual(os.environ["AGENT_MAX_TURNS"], "9")
            self.assertNotIn("AGENT_PROVIDER", os.environ)
        with self.assertRaises(ValueError):
            Settings(workdir=self.root, max_turns=0)
        with self.assertRaises(ValueError):
            Settings(workdir=self.root, provider="unknown")

    def test_live_adapter_payload_normalization_and_close(self):
        settings = Settings(workdir=self.root, provider="deepseek", model="test-model", api_key="test-key")
        raw = {"content": [{"type": "thinking", "thinking": "reasoning", "signature": "keep-me"},
                           {"type": "text", "text": "done"}], "stop_reason": "end_turn",
               "usage": {"input_tokens": 10, "output_tokens": 5}}
        with patch("anthropic.Anthropic") as sdk:
            sdk.return_value.messages.create.return_value = raw
            adapter = AnthropicLLM(settings)
            response = adapter.create(system="test system", messages=[{"role": "user", "content": "test"}],
                                      tools=self.runtime.tools.definitions(), max_tokens=123)
            self.assertEqual(sdk.call_args.kwargs["base_url"], "https://api.deepseek.com/anthropic")
            payload = sdk.return_value.messages.create.call_args.kwargs
            self.assertEqual(payload["model"], "test-model")
            self.assertEqual(payload["max_tokens"], 123)
            self.assertEqual({tool["name"] for tool in payload["tools"]},
                             {"list_dir", "read_file", "search_content", "write_file", "bash",
                              "search_news", "fetch_article"})
            self.assertEqual(response.content[0]["signature"], "keep-me")
            self.assertEqual(response.usage["input_tokens"], 10)
            adapter.close()
            sdk.return_value.close.assert_called_once()

    def test_hook_cannot_reenter_runtime(self):
        def reenter(block, ctx):
            ctx.runtime.call_tool("list_dir", path=".")
        self.runtime.hooks.register(PRE_TOOL_USE, reenter)
        result = self.runtime.call_tool("write_file", path="blocked.txt", content="x")
        self.assertEqual(result.error_code, "HOOK_ERROR")
        self.assertFalse((self.root / "blocked.txt").exists())


if __name__ == "__main__":
    unittest.main()
