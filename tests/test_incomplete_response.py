import tempfile
import unittest
from pathlib import Path

from agent import Runtime, Settings
from agent.events import POST_TOOL_USE, PRE_TOOL_USE, STOP, StopDirective
from agent.llm import LLMResponse, MockLLM


def text_response(text, reason="end_turn"):
    return LLMResponse([{"type": "text", "text": text}], reason)


def tool_response(path="discarded.txt", *, reason="max_tokens", call_id="partial"):
    return LLMResponse([{"type": "tool_use", "id": call_id, "name": "write_file",
                         "input": {"path": path, "content": "partial"}}], reason)


class IncompleteResponseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="newsagent-incomplete-")
        self.root = Path(self.temp.name)
        self.settings = Settings(workdir=self.root, memory_enabled=False)

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_whitespace_and_thinking_recover_without_history_or_early_stop(self):
        cases = [LLMResponse([]), text_response(" \n\t"),
                 LLMResponse([{"type": "thinking", "thinking": "unfinished", "signature": "partial"}]),
                 LLMResponse([], "tool_use"), LLMResponse([], "max_tokens")]
        for response in cases:
            with self.subTest(response=response), Runtime.create(self.settings, script=[response, "done"]) as runtime:
                stops = []
                runtime.hooks.register(STOP, lambda result, ctx: stops.append(result.text))
                result = runtime.submit("test")
                self.assertTrue(result.ok, result.error)
                self.assertEqual((result.turns, result.tool_calls, result.tool_attempts), (2, 0, 0))
                self.assertEqual(stops, ["done"])
                self.assertEqual(runtime.llm.calls[1]["messages"], [{"role": "user", "content": "test"}])
                self.assertIn("[agent recovery]", runtime.llm.calls[1]["system"])
                self.assertEqual(len(runtime.messages), 2)

    def test_three_empty_responses_exhaust_budget_without_stop_memory_or_archive(self):
        memory_llm = MockLLM(script=[])
        settings = Settings(workdir=self.root)
        with Runtime.create(settings, script=[LLMResponse([])] * 3, memory_llm=memory_llm) as runtime:
            runtime.update_profile(identity="工程师", preferences="关注开源模型")
            stops = []
            runtime.hooks.register(STOP, lambda *_: stops.append(True))
            generated = runtime.generate_brief()
            self.assertFalse(generated.ok)
            self.assertEqual(generated.result.stop_reason, "incomplete")
            self.assertIn("after 2 retries", generated.result.error)
            self.assertEqual(generated.result.turns, 3)
            self.assertEqual(len(runtime.llm.calls), 3)
            self.assertEqual(stops, [])
            self.assertEqual(len(memory_llm.calls), 0)
            self.assertFalse(runtime._memory_pending)
            self.assertIsNone(generated.brief)
            self.assertFalse(runtime.briefs.directory.exists())
            self.assertEqual(len(runtime.messages), 1)

    def test_empty_text_and_tool_truncation_share_budget(self):
        script = [LLMResponse([]), text_response("draft", "max_tokens"), tool_response()]
        with Runtime.create(self.settings, script=script) as runtime:
            result = runtime.submit("test")
            self.assertEqual((result.stop_reason, result.turns, result.tool_calls), ("incomplete", 3, 0))
            self.assertEqual(len(runtime.llm.calls), 3)
            self.assertFalse((self.root / "discarded.txt").exists())

    def test_entire_truncated_batch_discarded_even_with_missing_or_duplicate_ids(self):
        for call_id in (None, "duplicate"):
            response = tool_response(call_id=call_id)
            response.content.append({"type": "tool_use", "id": call_id, "name": "bash",
                                     "input": {"command": "should never execute"}})
            with self.subTest(call_id=call_id), Runtime.create(self.settings, script=[response] * 3) as runtime:
                hooks = []
                runtime.hooks.register(PRE_TOOL_USE, lambda *_: hooks.append("pre"))
                runtime.hooks.register(POST_TOOL_USE, lambda *_: hooks.append("post"))
                result = runtime.submit("test")
                self.assertEqual(result.stop_reason, "incomplete")
                self.assertEqual((result.tool_calls, result.tool_attempts), (0, 0))
                self.assertEqual(runtime.recovery.events, [])
                self.assertEqual(hooks, [])
                self.assertEqual(len(runtime.messages), 1)
                self.assertFalse((self.root / "discarded.txt").exists())

    def test_truncated_tools_regenerated_and_prior_completed_work_retained(self):
        script = [{"tool": "write_file", "input": {"path": "earlier.txt", "content": "earlier"}},
                  tool_response(),
                  {"tool": "write_file", "input": {"path": "complete.txt", "content": "complete"}}, "done"]
        events = []
        with Runtime.create(self.settings, script=script, on_event=events.append) as runtime:
            result = runtime.submit("test")
            self.assertTrue(result.ok, result.error)
            self.assertEqual((result.turns, result.tool_calls, result.tool_attempts), (4, 2, 2))
            self.assertEqual((self.root / "earlier.txt").read_text(), "earlier")
            self.assertEqual((self.root / "complete.txt").read_text(), "complete")
            self.assertFalse((self.root / "discarded.txt").exists())
            retry_call = runtime.llm.calls[2]
            self.assertEqual(retry_call["messages"], runtime.llm.calls[1]["messages"])
            self.assertIn("None of that response's tool calls were executed", retry_call["system"])
            self.assertIn("earlier completed tool results remain valid", retry_call["system"])
            self.assertNotIn("[agent recovery]", runtime.llm.calls[3]["system"])
            self.assertTrue(any("[model recovery]" in event for event in events))
            uses = [block["id"] for msg in runtime.messages if isinstance(msg["content"], list)
                    for block in msg["content"] if block["type"] == "tool_use"]
            results = [block["tool_use_id"] for msg in runtime.messages if isinstance(msg["content"], list)
                       for block in msg["content"] if block["type"] == "tool_result"]
            self.assertEqual(uses, results)
            self.assertNotIn("partial", uses)

    def test_truncated_text_rewritten_and_only_complete_final_brief_archived(self):
        complete = "# 今日简报\n\n第一条报道：[来源一](https://example.com/one)。\n\n第二条报道：[来源二](https://example.com/two)。\n"
        partial = LLMResponse([{"type": "thinking", "thinking": "partial reasoning", "signature": "unfinished"},
                               {"type": "text", "text": "# 今日简报\n\n第一条报道：[来源一](https://example.com/one)。\n\n第二条"}], "max_tokens")
        with Runtime.create(self.settings, script=[partial, complete]) as runtime:
            stops = []
            runtime.hooks.register(STOP, lambda result, ctx: stops.append(result.text))
            generated = runtime.generate_brief()
            self.assertTrue(generated.ok, generated.result.error)
            self.assertEqual(stops, [complete])
            self.assertEqual(generated.result.text, complete)
            self.assertEqual(runtime.briefs.get(generated.brief["id"])["content"], complete)
            self.assertEqual(generated.brief["turns"], 2)
            self.assertEqual(len(runtime.briefs.list()), 1)
            retry_call = runtime.llm.calls[1]
            self.assertIn("from the beginning", retry_call["system"])
            self.assertIn("do not return only the remaining tail", retry_call["messages"][-1]["content"])
            self.assertEqual(retry_call["messages"][-2]["content"], [partial.content[1]])

    def test_repeated_text_truncation_does_not_concatenate_drafts(self):
        script = [text_response("draft one", "max_tokens"), text_response("draft two", "max_tokens"), "full answer"]
        with Runtime.create(self.settings, script=script) as runtime:
            generated = runtime.generate_brief()
            self.assertTrue(generated.ok)
            self.assertEqual(generated.result.turns, 3)
            self.assertEqual(runtime.briefs.get(generated.brief["id"])["content"], "full answer")

    def test_text_rewrite_instruction_survives_intermediate_valid_tool_call(self):
        script = [text_response("draft", "max_tokens"),
                  {"tool": "list_dir", "input": {"path": "."}}, "complete answer"]
        with Runtime.create(self.settings, script=script) as runtime:
            result = runtime.submit("test")
            self.assertTrue(result.ok, result.error)
            self.assertEqual((result.turns, result.tool_calls), (3, 1))
            self.assertNotIn("[agent recovery]", runtime.llm.calls[2]["system"])
            self.assertTrue(any(isinstance(msg["content"], str) and "from the beginning" in msg["content"]
                                for msg in runtime.llm.calls[2]["messages"]))

    def test_budget_resets_after_valid_tool_response(self):
        script = [LLMResponse([]), LLMResponse([]), {"tool": "list_dir", "input": {"path": "."}},
                  LLMResponse([]), LLMResponse([]), "done"]
        with Runtime.create(self.settings, script=script) as runtime:
            result = runtime.submit("test")
            self.assertTrue(result.ok, result.error)
            self.assertEqual((result.turns, result.tool_calls, result.tool_attempts), (6, 1, 1))
            self.assertIn("[agent recovery]", runtime.llm.calls[2]["system"])
            self.assertNotIn("[agent recovery]", runtime.llm.calls[3]["system"])
            self.assertEqual(runtime.stats.turns, 6)

    def test_new_request_gets_fresh_recovery_budget_and_system(self):
        with Runtime.create(self.settings, script=[LLMResponse([])] * 4 + ["done"]) as runtime:
            self.assertEqual(runtime.submit("first").stop_reason, "incomplete")
            result = runtime.submit("second")
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.turns, 2)
            self.assertNotIn("[agent recovery]", runtime.llm.calls[3]["system"])
            self.assertEqual(runtime.stats.turns, 5)

    def test_recovery_respects_max_turns_and_does_not_save_or_stop(self):
        for limit in (1, 2):
            for response in (LLMResponse([]), text_response("draft", "max_tokens"), tool_response()):
                with self.subTest(limit=limit, response=response):
                    settings = Settings(workdir=self.root, memory_enabled=False, max_turns=limit)
                    with Runtime.create(settings, script=[response] * 3) as runtime:
                        stops = []
                        runtime.hooks.register(STOP, lambda *_: stops.append(True))
                        generated = runtime.generate_brief()
                        self.assertEqual(generated.result.stop_reason, "max_turns")
                        self.assertEqual(generated.result.turns, limit)
                        self.assertEqual(len(runtime.llm.calls), limit)
                        self.assertEqual(stops, [])
                        self.assertIsNone(generated.brief)
                        self.assertEqual(generated.result.tool_attempts, 0)
        self.assertFalse((self.root / "discarded.txt").exists())
        self.assertFalse((self.root / ".agent/briefs").exists())

    def test_refusal_and_pause_turn_are_not_retried_or_executed(self):
        for reason, expected in (("refusal", "blocked"), ("pause_turn", "incomplete")):
            with self.subTest(reason=reason):
                with Runtime.create(self.settings, script=[tool_response(reason=reason, call_id=None)]) as runtime:
                    generated = runtime.generate_brief()
                    self.assertEqual(generated.result.stop_reason, expected)
                    self.assertEqual(len(runtime.llm.calls), 1)
                    self.assertEqual(generated.result.tool_calls, 0)
                    self.assertIsNone(generated.brief)
        self.assertFalse((self.root / "discarded.txt").exists())

    def test_recovery_after_stop_continue_only_archives_accepted_final(self):
        with Runtime.create(self.settings, script=["first", LLMResponse([]), "final"]) as runtime:
            stops = []
            def review(result, ctx):
                stops.append(result.text)
                return StopDirective("continue", "补充来源") if len(stops) == 1 else None
            runtime.hooks.register(STOP, review)
            generated = runtime.generate_brief()
            self.assertTrue(generated.ok)
            self.assertEqual(stops, ["first", "final"])
            self.assertEqual(runtime.briefs.get(generated.brief["id"])["content"], "final")
            self.assertEqual(len(runtime.briefs.list()), 1)

    def test_memory_extract_runs_once_after_successful_recovery(self):
        memory_llm = MockLLM(script=["[]"])
        with Runtime.create(Settings(workdir=self.root), script=[LLMResponse([]), "done"],
                            memory_llm=memory_llm) as runtime:
            result = runtime.submit("以后关注开源模型")
            self.assertTrue(result.ok, result.error)
            self.assertEqual(len(memory_llm.calls), 1)
            self.assertIn("done", memory_llm.calls[0]["messages"][0]["content"])

    def test_recovery_progress_callback_failure_does_not_abort(self):
        def progress(message):
            if "[model recovery]" in message:
                raise RuntimeError("progress failed")
        with Runtime.create(self.settings, script=[LLMResponse([]), "done"], on_event=progress) as runtime:
            self.assertTrue(runtime.submit("test").ok)
            self.assertIn("progress failed", runtime.ctx.extra["hook_errors"][0])


if __name__ == "__main__":
    unittest.main()
