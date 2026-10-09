import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import Runtime, Settings, load_settings
from agent.cli import main
from agent.events import STOP, StopDirective
from agent.llm import LLMResponse, MockLLM, extract_text
from agent.memory import MemoryStore, memory_document, parse_frontmatter
from agent.onboarding import ensure_profile, show_profile


def candidate(name, body, evidence, *, scope="persistent", mem_type="user"):
    return {"name": name, "type": mem_type, "scope": scope,
            "description": "stable user information", "body": body, "evidence": evidence}


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="newsagent-memory-")
        self.root = Path(self.temp.name)
        self.settings = Settings(workdir=self.root)
        self.runtime = Runtime.create(self.settings)

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def store(self, script=None, responder=None):
        llm = MockLLM(script=script, responder=responder)
        return MemoryStore(llm, "mock-1", self.root / ".agent/memory", self.root)

    def test_first_setup_persists_markdown_and_restart_skips_questions(self):
        replies = iter(["AI 产品经理，了解基础机器学习", "关注开源模型和应用；中文，每天五条，附来源"])
        questions, output = [], []
        def answer(question):
            questions.append(question)
            return next(replies)
        self.assertTrue(ensure_profile(self.runtime, input_fn=answer, output_fn=output.append))
        directory = self.settings.memory_dir
        self.assertTrue((directory / "user-profile.md").exists())
        self.assertTrue((directory / "news-preferences.md").exists())
        self.assertIn("user-profile.md", (directory / "MEMORY.md").read_text(encoding="utf-8"))
        with Runtime.create(self.settings) as restarted:
            self.assertEqual(restarted.missing_profile_fields, [])
            self.assertFalse(ensure_profile(restarted, input_fn=lambda _: self.fail("asked again")))
            self.assertIn("AI 产品经理", restarted.build_system())
            self.assertIn("每天五条", restarted.build_system())
            self.assertEqual(len(restarted.memory_llm.calls), 0)
        self.assertEqual(len(questions), 2)

    def test_partial_profile_only_asks_missing_field(self):
        self.runtime.update_profile(identity="研究生")
        questions = []
        def answer(question):
            questions.append(question)
            return "关注研究论文，详细中文解释"
        ensure_profile(self.runtime, input_fn=answer, output_fn=lambda _: None)
        self.assertEqual(len(questions), 1)
        self.assertIn("主题", questions[0])
        self.assertEqual(self.runtime.memory.profile()["identity"], "研究生")

    def test_skip_and_eof_do_not_write_partial_answers(self):
        replies = iter(["工程师", "/skip"])
        ensure_profile(self.runtime, input_fn=lambda _: next(replies), output_fn=lambda _: None)
        self.assertFalse(self.settings.memory_dir.exists())
        self.assertIn("不重复追问", self.runtime.build_system())
        def eof(_):
            raise EOFError
        ensure_profile(self.runtime, input_fn=eof, output_fn=lambda _: None)
        self.assertEqual(self.runtime.missing_profile_fields, ["identity", "preferences"])

    def test_edit_preserves_blank_fields_and_overwrites_fixed_record(self):
        self.runtime.update_profile(identity="工程师", preferences="关注模型；中文")
        replies = iter(["", "关注模型和应用；中文"])
        ensure_profile(self.runtime, edit=True, input_fn=lambda _: next(replies), output_fn=lambda _: None)
        profile = self.runtime.memory.profile()
        self.assertEqual(profile["identity"], "工程师")
        self.assertEqual(profile["preferences"], "关注模型和应用；中文")
        self.assertEqual(len(self.runtime.memory.list_records()), 2)
        output = []
        show_profile(self.runtime, output_fn=output.append)
        self.assertIn("工程师", output[0])

    def test_profile_validation_precedes_any_write(self):
        with self.assertRaises(ValueError):
            self.runtime.update_profile(identity="工程师", preferences=" " )
        self.assertFalse(self.settings.memory_dir.exists())
        with self.assertRaises(ValueError):
            self.runtime.update_profile(identity="x" * 4001)

    def test_core_always_loaded_when_selector_returns_empty(self):
        memory_llm = MockLLM(script=["[]"])
        with Runtime.create(self.settings, memory_llm=memory_llm) as runtime:
            runtime.update_profile(identity="研究员", preferences="论文与开源，中文")
            runtime.memory.write_record("other reference", "reference", "unrelated", "Other details")
            runtime.messages = [{"role": "user", "content": "生成今天的简报"}]
            system = runtime.build_system()
            self.assertIn("研究员", system)
            self.assertIn("论文与开源", system)
            self.assertNotIn("Other details", system)
            self.assertEqual(len(memory_llm.calls), 1)

    def test_selection_cached_across_tool_rounds_and_reselected_next_request(self):
        memory_llm = MockLLM(script=["[0]", "[]", "[0]", "[]"])
        script = [{"tool": "list_dir", "input": {"path": "."}}, "done", "done again"]
        with Runtime.create(self.settings, script=script, memory_llm=memory_llm) as runtime:
            runtime.update_profile(identity="Engineer", preferences="Open source news")
            runtime.memory.write_record("source notes", "reference", "news sources", "reference details")
            self.assertTrue(runtime.submit("news please").ok)
            self.assertEqual(len(memory_llm.calls), 2)  # one selection + one extraction
            self.assertEqual(len(runtime.llm.calls), 2)
            self.assertIn("reference details", runtime.llm.calls[0]["system"])
            self.assertIn("reference details", runtime.llm.calls[1]["system"])
            self.assertTrue(runtime.submit("news again").ok)
            self.assertEqual(len(memory_llm.calls), 4)

    def test_explicit_long_term_preference_change_updates_existing_record(self):
        request = "以后多关注开源模型，少看融资新闻"
        updated = "中文简报，每天五条；关注开源模型，减少融资新闻"
        memory_llm = MockLLM(script=[json.dumps([candidate("news-preferences", updated, request)])])
        with Runtime.create(self.settings, script=["好的。"], memory_llm=memory_llm) as runtime:
            runtime.update_profile(identity="工程师", preferences="中文简报，每天五条；关注融资新闻")
            self.assertTrue(runtime.submit(request).ok)
            self.assertEqual(runtime.memory.profile()["preferences"], updated)
            self.assertEqual(len(runtime.memory.list_records()), 2)
            extraction_prompt = memory_llm.calls[0]["messages"][0]["content"]
            self.assertIn("中文简报，每天五条", extraction_prompt)
            self.assertIn("preserve existing details", extraction_prompt)
            with Runtime.create(self.settings) as restarted:
                self.assertIn("减少融资新闻", restarted.build_system())

    def test_identity_change_updates_fixed_identity_record(self):
        request = "我现在转做 AI 产品经理了"
        memory_llm = MockLLM(script=[json.dumps([candidate("user-profile", "AI 产品经理", request)])])
        with Runtime.create(self.settings, script=["好的"], memory_llm=memory_llm) as runtime:
            runtime.update_profile(identity="后端工程师", preferences="应用新闻")
            runtime.submit(request)
            self.assertEqual(runtime.memory.profile()["identity"], "AI 产品经理")

    def test_temporary_request_does_not_change_long_term_preferences(self):
        request = "今天只看开源模型"
        # Even a mistaken persistent label is rejected by the evidence markers.
        memory_llm = MockLLM(script=[json.dumps([candidate("news-preferences", "只看开源模型", request)])])
        with Runtime.create(self.settings, script=["今天按这个范围处理"], memory_llm=memory_llm) as runtime:
            runtime.update_profile(identity="工程师", preferences="模型与行业动态")
            runtime.submit(request)
            self.assertEqual(runtime.memory.profile()["preferences"], "模型与行业动态")

    def test_extraction_requires_user_evidence_and_persistent_scope(self):
        request = "我长期关注模型评测"
        records = [candidate("bad assumption", "猜测用户偏好", "用户没有说过"),
                   candidate("task detail", "one-off", request, scope="current_task"),
                   candidate("stable interest", "模型评测", request)]
        store = self.store(script=[json.dumps(records)])
        self.assertEqual(store.extract([{"role": "user", "content": request},
                                        {"role": "assistant", "content": "用户没有说过"}]), 1)
        self.assertEqual([record["name"] for record in store.list_records()], ["stable interest"])

    def test_core_updates_are_independent_of_other_record_descriptions(self):
        user = "我现在是产品经理，以后看应用新闻"
        store = self.store(script=[json.dumps([
            candidate("user-profile", "产品经理", user),
            candidate("news-preferences", "应用新闻", user),
        ])])
        store.write_record("news-preferences", "user", "stable user information", "模型新闻")
        store.write_record("user-profile", "user", "stable user information", "工程师")
        self.assertEqual(store.extract([{"role": "user", "content": user}]), 2)
        self.assertEqual(store.profile(), {"identity": "产品经理", "preferences": "应用新闻"})

    def test_incomplete_memory_response_never_writes(self):
        user = "以后看应用新闻"
        partial = LLMResponse([{"type": "text", "text": json.dumps([
            candidate("news-preferences", "应用新闻", user)])}], "max_tokens")
        store = self.store(script=[partial])
        store.set_profile(identity="工程师", preferences="论文")
        self.assertEqual(store.extract([{"role": "user", "content": user}]), 0)
        self.assertEqual(store.profile()["preferences"], "论文")

    def test_stop_continuation_extracts_once_after_final_response(self):
        memory_llm = MockLLM(script=["[]"])
        with Runtime.create(self.settings, script=["draft", "final"], memory_llm=memory_llm) as runtime:
            runtime.hooks.register(STOP, lambda result, ctx:
                                   StopDirective("continue", "check again") if result.turns == 1 else None)
            result = runtime.submit("test")
            self.assertTrue(result.ok)
            self.assertEqual(result.turns, 2)
            self.assertEqual(len(memory_llm.calls), 1)
            self.assertIn("final", memory_llm.calls[0]["messages"][0]["content"])

    def test_error_blocked_and_budget_exhaustion_do_not_extract(self):
        cases = [([], None, "error"),
                 (["done"], lambda *_: StopDirective("return", "blocked", "blocked"), "blocked"),
                 (["done"], lambda *_: StopDirective("continue", "again"), "max_turns")]
        for script, callback, reason in cases:
            with self.subTest(reason=reason):
                memory_llm = MockLLM(script=[])
                with Runtime.create(Settings(workdir=self.root, max_turns=1), script=script,
                                    memory_llm=memory_llm) as runtime:
                    if callback:
                        runtime.hooks.register(STOP, callback)
                    self.assertEqual(runtime.submit("test").stop_reason, reason)
                    self.assertEqual(len(memory_llm.calls), 0)

    def test_only_current_request_dialogue_is_extracted(self):
        memory_llm = MockLLM(script=["[]", "[]"])
        with Runtime.create(self.settings, script=["first answer", "second answer"], memory_llm=memory_llm) as runtime:
            runtime.submit("old request marker")
            runtime.submit("new request marker")
            text = memory_llm.calls[1]["messages"][0]["content"]
            self.assertIn("new request marker", text)
            self.assertNotIn("old request marker", text)

    def test_extraction_failure_does_not_break_response(self):
        def broken(*args):
            raise ConnectionError("memory provider offline")
        with Runtime.create(self.settings, script=["done"], memory_llm=MockLLM(responder=broken)) as runtime:
            self.assertTrue(runtime.submit("hello").ok)
            self.assertTrue(any("extraction skipped" in note for note in runtime.memory.drain_notes()))

    def test_auto_extract_disabled_keeps_profile_injection_and_manual_update(self):
        settings = Settings(workdir=self.root, memory_auto_extract=False)
        memory_llm = MockLLM(script=[])
        with Runtime.create(settings, script=["done"], memory_llm=memory_llm) as runtime:
            runtime.update_profile(identity="工程师", preferences="论文新闻")
            self.assertTrue(runtime.submit("hello").ok)
            self.assertIn("论文新闻", runtime.llm.calls[0]["system"])
            self.assertEqual(len(memory_llm.calls), 0)
            runtime.update_profile(preferences="开源新闻")
            self.assertIn("开源新闻", runtime.build_system())

    def test_memory_disabled_has_no_reads_writes_or_bookkeeping_calls(self):
        settings = Settings(workdir=self.root, memory_enabled=False)
        memory_llm = MockLLM(script=[])
        with Runtime.create(settings, script=["done"], memory_llm=memory_llm) as runtime:
            self.assertEqual(runtime.missing_profile_fields, [])
            self.assertTrue(runtime.submit("hello").ok)
            self.assertEqual(len(memory_llm.calls), 0)
            self.assertNotIn("Memory catalog", runtime.llm.calls[0]["system"])
            with self.assertRaises(ValueError):
                runtime.update_profile(identity="x")
            self.assertFalse(settings.memory_dir.exists())

    def test_reset_keeps_disk_memory_and_invalidates_cache(self):
        self.runtime.update_profile(identity="工程师", preferences="模型新闻")
        self.runtime.submit("hello")
        self.runtime.reset()
        self.assertEqual(self.runtime.messages, [])
        self.assertIn("模型新闻", self.runtime.build_system())

    def test_consolidation_keeps_core_records_byte_for_byte(self):
        merged = {"name": "merged reference", "type": "reference", "description": "merged", "body": "combined details"}
        store = self.store(script=[json.dumps([merged])])
        store.set_profile(identity="工程师", preferences="开源，中文")
        before = {field: store.read_record(field) for field in ("user-profile.md", "news-preferences.md")}
        for index in range(10):
            store.write_record(f"note {index}", "reference", f"note {index}", f"details {index}")
        self.assertEqual(store.consolidate(), 1)
        self.assertEqual(len(store.list_records()), 3)
        for field, document in before.items():
            self.assertEqual(store.read_record(field), document)
        self.assertNotIn("开源，中文", store.llm.calls[0]["messages"][0]["content"])

    def test_consolidation_cannot_overwrite_core_and_rolls_back_write_failure(self):
        malicious = {"name": "news-preferences", "type": "user", "description": "oops", "body": "changed"}
        merged = {"name": "merged", "type": "reference", "description": "merged", "body": "combined"}
        store = self.store(script=[json.dumps([malicious]), json.dumps([merged])])
        store.set_profile(identity="工程师", preferences="开源")
        for index in range(10):
            store.write_record(f"note {index}", "reference", f"note {index}", f"details {index}")
        before = {record["filename"]: store.read_record(record["filename"]) for record in store.list_records()}
        self.assertEqual(store.consolidate(), 0)
        original_write = store._write_document
        def fail_merged(filename, content, **kwargs):
            if filename == "merged.md":
                raise OSError("simulated failed replacement")
            return original_write(filename, content, **kwargs)
        with patch.object(store, "_write_document", side_effect=fail_merged):
            self.assertEqual(store.consolidate(), 0)
        after = {record["filename"]: store.read_record(record["filename"]) for record in store.list_records()}
        self.assertEqual(before, after)

    def test_selector_failure_falls_back_to_keywords_but_empty_array_is_respected(self):
        store = self.store(script=["bad response", "[]", "[false]"])
        store.write_record("open source", "reference", "open source models", "details")
        messages = [{"role": "user", "content": "open source"}]
        self.assertEqual(store.select_relevant(messages), ["open-source.md"])
        self.assertEqual(store.select_relevant(messages), [])
        self.assertEqual(store.select_relevant(messages), [])

    def test_memory_path_escape_reserved_index_and_atomic_write(self):
        store = self.store()
        for filename in ("../escape.md", "sub/file.md", "sub\\file.md", "MEMORY.md", "memory.md"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                store.memory_path(filename)
        store.set_profile(identity="old", preferences="models")
        before = store.read_record("user-profile.md")
        with patch("agent.memory.os.replace", side_effect=OSError("disk write failed")):
            with self.assertRaises(OSError):
                store.set_profile(identity="new")
        self.assertEqual(store.read_record("user-profile.md"), before)
        self.assertEqual(list(store.memory_dir.glob(".memory-*")), [])

    def test_frontmatter_and_text_extraction_preserve_content(self):
        text = memory_document("test --- name", "user", "冒号：与 --- 分隔符", "正文\n---\n后续")
        metadata, body = parse_frontmatter(text)
        self.assertEqual(metadata["name"], "test --- name")
        self.assertIn("正文\n---\n后续", body)
        self.assertEqual(extract_text("hello"), "hello")
        self.assertEqual(extract_text([{"type": "text", "text": "hello"},
                                       {"type": "tool_result", "content": "do not extract"}]), "hello")

    def test_config_memory_flags_directory_and_precedence(self):
        env = self.root / "settings.env"
        env.write_text("AGENT_MEMORY_ENABLED=false\nAGENT_MEMORY_AUTO_EXTRACT=false\nAGENT_MEMORY_DIR=.agent/custom-memory\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(env, workdir=self.root, memory_enabled=True)
            self.assertTrue(settings.memory_enabled)
            self.assertFalse(settings.memory_auto_extract)
            self.assertEqual(settings.memory_dir, self.root / ".agent/custom-memory")
        for directory in ("..", "."):
            with self.assertRaises(ValueError):
                Settings(workdir=self.root, memory_dir=directory)

    def test_cli_interactive_first_and_second_launch(self):
        argv = ["--provider", "mock", "--workdir", str(self.root)]
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["工程师", "开源模型，中文", "/exit"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(argv), 0)
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["/profile", "/exit"]) as inputs, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(argv), 0)
            self.assertEqual(inputs.call_count, 2)
            self.assertIn("开源模型，中文", output.getvalue())

    def test_cli_json_is_noninteractive_and_demo_does_not_create_memory(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=AssertionError("unexpected input")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["hello", "--json", "--provider", "mock", "--workdir", str(self.root)]), 0)
            self.assertEqual(json.loads(output.getvalue())["stop_reason"], "final")
        with patch("builtins.input", side_effect=AssertionError("unexpected input")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--demo", "--workdir", str(self.root)]), 0)
        self.assertFalse(self.settings.memory_dir.exists())


if __name__ == "__main__":
    unittest.main()
