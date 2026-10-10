import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent import BriefStore, Runtime, Settings, load_settings
from agent.briefs import SHANGHAI
from agent.cli import main
from agent.events import STOP, USER_PROMPT_SUBMIT, StopDirective
from agent.llm import LLMResponse, MockLLM


class BriefTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="newsagent-brief-")
        self.root = Path(self.temp.name)
        self.settings = Settings(workdir=self.root, memory_enabled=False)
        self.store = BriefStore(self.root)
        self.content = "# 今日 AI 新闻\n\n一条模拟新闻：[来源](https://example.com/news)。\n"

    def tearDown(self):
        self.temp.cleanup()

    def test_lazy_archive_and_round_trip_after_restart(self):
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.store.directory.exists())
        saved = self.store.save(self.content, provider="mock", model="mock-1", turns=2,
                                tool_calls=1, tool_attempts=3)
        path = self.root / saved["path"]
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("title: 今日 AI 新闻", text)
        self.assertTrue(text.endswith(self.content))
        self.assertEqual(saved["title"], "今日 AI 新闻")
        self.assertEqual(saved["content_bytes"], len(self.content.encode("utf-8")))
        self.assertTrue(saved["created_at"].endswith("+08:00"))
        restarted = BriefStore(self.root)
        read = restarted.get(saved["id"])
        self.assertEqual(read["content"], self.content)
        self.assertEqual(read["tool_attempts"], 3)
        self.assertEqual(restarted.list(), [saved])
        self.assertEqual(list(self.store.directory.glob(".brief-*")), [])

    def test_same_second_and_collision_never_overwrite(self):
        clock = datetime(2026, 10, 10, 8, 0, 0, tzinfo=SHANGHAI)
        with patch("agent.briefs.datetime") as current, patch("agent.briefs.uuid.uuid4") as uid:
            current.now.return_value = clock
            uid.return_value.hex = "1" * 32
            first = self.store.save("first")
            uid.side_effect = [type("ID", (), {"hex": "1" * 32})(), type("ID", (), {"hex": "2" * 32})()]
            second = self.store.save("second")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.store.get(first["id"])["content"], "first")
        self.assertEqual(self.store.get(second["id"])["content"], "second")
        self.assertEqual(len(list(self.store.directory.glob("*.md"))), 2)
        self.assertEqual(list(self.store.directory.glob(".brief-*")), [])

    def test_history_order_pagination_and_metadata_only(self):
        for day in (9, 10, 11):
            with patch("agent.briefs.datetime") as current:
                current.now.return_value = datetime(2026, 10, day, 8, tzinfo=SHANGHAI)
                self.store.save(self.content)
        page = self.store.list(limit=2)
        self.assertTrue(page[0]["id"].startswith("2026-10-11"))
        self.assertTrue(page[1]["id"].startswith("2026-10-10"))
        self.assertNotIn("content", page[0])
        remaining = self.store.list(before=page[-1]["id"])
        self.assertEqual(len(remaining), 1)
        self.assertTrue(remaining[0]["id"].startswith("2026-10-09"))
        for invalid in (0, 101, True, 2.5):
            with self.assertRaises(ValueError):
                self.store.list(limit=invalid)

    def test_write_failure_cleans_temporary_file(self):
        with patch("agent.briefs.os.link", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.save(self.content)
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_invalid_content_titles_and_size_have_no_files(self):
        for content in ("", "   ", None, "a\x00b"):
            with self.assertRaises(ValueError):
                self.store.save(content)
        for title in ("", "a\nb", "x" * 201, 42):
            with self.assertRaises(ValueError):
                self.store.save(self.content, title=title)
        with self.assertRaises(ValueError):
            BriefStore(self.root, max_content_bytes=3).save("中文")
        self.assertFalse(self.store.directory.exists())

    def test_ids_and_storage_paths_cannot_escape(self):
        for brief_id in ("../private", "a.md", "", None):
            with self.assertRaises(ValueError):
                self.store.get(brief_id)
        with self.assertRaises(PermissionError):
            BriefStore(self.root, self.root.parent)
        with self.assertRaises(ValueError):
            Settings(workdir=self.root, briefs_dir="..")
        for directory in (".agent/memory", ".agent", ".agent/memory/briefs"):
            with self.assertRaises(ValueError):
                Settings(workdir=self.root, briefs_dir=directory)

    def test_directory_symlink_escape_is_rechecked(self):
        with tempfile.TemporaryDirectory(prefix="newsagent-brief-outside-") as outside:
            directory = self.root / "archive"
            store = BriefStore(self.root, directory)
            try:
                directory.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("host does not permit symlink creation")
            with self.assertRaises(PermissionError):
                store.save(self.content)
            with self.assertRaises(PermissionError):
                store.list()
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_corrupt_metadata_skipped_and_incomplete_body_rejected(self):
        saved = self.store.save(self.content)
        broken = self.store.directory / "2026-10-10_000000_aaaaaaaa.md"
        broken.write_text("---\nid: wrong\n---\nbody", encoding="utf-8")
        self.assertEqual(self.store.list(), [saved])
        with self.assertRaises(ValueError):
            self.store.get(broken.stem)
        path = self.root / saved["path"]
        path.write_bytes(path.read_bytes()[:-2])
        with self.assertRaises(ValueError):
            self.store.get(saved["id"])

    def test_generate_archives_final_text_only_and_retains_history_on_reset(self):
        with Runtime.create(self.settings, script=[self.content]) as runtime:
            generated = runtime.generate_brief()
            self.assertTrue(generated.ok, generated.save_error)
            self.assertEqual(runtime.briefs.get(generated.brief["id"])["content"], generated.result.text)
            self.assertEqual(generated.brief["turns"], 1)
            self.assertIn("Asia/Shanghai", runtime.llm.calls[0]["messages"][0]["content"])
            runtime.reset()
            self.assertEqual(len(runtime.briefs.list()), 1)

    def test_ordinary_submit_does_not_archive(self):
        with Runtime.create(self.settings, script=["普通回复"]) as runtime:
            self.assertTrue(runtime.submit("hello").ok)
            self.assertEqual(runtime.briefs.list(), [])
            self.assertFalse(runtime.briefs.directory.exists())

    def test_news_tool_loop_archives_final_brief_with_sources_and_stats(self):
        url = "https://example.com/news"
        search = {"results": [{"url": url, "title": "模拟模型发布", "content": "搜索片段",
                               "published_date": "2026-10-10", "score": 0.9}]}
        extracted = {"results": [{"url": url, "raw_content": "完整模拟新闻正文"}]}
        opener = MagicMock()
        opener.open.side_effect = [io.BytesIO(json.dumps(data).encode("utf-8"))
                                   for data in (search, extracted)]
        settings = Settings(workdir=self.root, memory_enabled=False, tavily_api_key="offline-test-key")
        script = [{"tool": "search_news", "input": {"query": "AI 模型发布"}},
                  {"tool": "fetch_article", "input": {"url": url}}, self.content]
        with patch("agent.tools.news.build_opener", return_value=opener), Runtime.create(settings, script=script) as runtime:
            generated = runtime.generate_brief("生成简报")
            self.assertTrue(generated.ok, generated.result.error or generated.save_error)
            self.assertEqual((generated.brief["turns"], generated.brief["tool_calls"],
                              generated.brief["tool_attempts"]), (3, 2, 2))
            archived = runtime.briefs.get(generated.brief["id"])
            self.assertEqual(archived["content"], self.content)
            self.assertIn(url, archived["content"])
            self.assertNotIn("offline-test-key", (self.root / generated.brief["path"]).read_text(encoding="utf-8"))
            self.assertNotIn("搜索片段", archived["content"])
            self.assertEqual(opener.open.call_count, 2)

    def test_empty_final_text_is_not_archived(self):
        with Runtime.create(self.settings, script=[self.content]) as runtime:
            runtime.hooks.register(STOP, lambda result, ctx: setattr(result, "text", ""))
            generated = runtime.generate_brief()
            self.assertTrue(generated.result.ok)
            self.assertFalse(generated.ok)
            self.assertIsNone(generated.brief)
            self.assertIn("nonempty", generated.save_error)
            self.assertFalse(runtime.briefs.directory.exists())

    def test_failed_blocked_incomplete_and_exhausted_requests_are_not_archived(self):
        cases = [LLMResponse([{"type": "text", "text": "拒绝"}], "refusal"),
                 LLMResponse([{"type": "text", "text": "未完成"}], "max_tokens")]
        for response in cases:
            with Runtime.create(self.settings, script=[response]) as runtime:
                generated = runtime.generate_brief("生成简报")
                self.assertFalse(generated.ok)
                self.assertIsNone(generated.brief)
        with Runtime.create(self.settings, script=[]) as runtime:
            self.assertFalse(runtime.generate_brief().ok)
        settings = Settings(workdir=self.root, memory_enabled=False, max_turns=1)
        with Runtime.create(settings, script=[{"tool": "list_dir", "input": {"path": "."}}]) as runtime:
            self.assertEqual(runtime.generate_brief().result.stop_reason, "max_turns")
        with Runtime.create(self.settings, script=[self.content]) as runtime:
            runtime.hooks.register(USER_PROMPT_SUBMIT, lambda *_: "blocked")
            self.assertFalse(runtime.generate_brief().ok)
        self.assertFalse(self.store.directory.exists())

    def test_stop_continue_archives_only_final_reply(self):
        with Runtime.create(self.settings, script=["草稿", self.content]) as runtime:
            count = []
            def review(result, ctx):
                count.append(result.text)
                return StopDirective("continue", "请补充来源") if len(count) == 1 else None
            runtime.hooks.register(STOP, review)
            generated = runtime.generate_brief("生成简报")
            self.assertTrue(generated.ok)
            self.assertEqual(generated.result.turns, 2)
            self.assertEqual(len(runtime.briefs.list()), 1)
            self.assertEqual(runtime.briefs.get(generated.brief["id"])["content"], self.content)

    def test_save_failure_keeps_generated_content_and_does_not_repeat_model(self):
        with Runtime.create(self.settings, script=[self.content]) as runtime:
            with patch.object(runtime.briefs, "save", side_effect=OSError("disk unavailable")):
                generated = runtime.generate_brief("生成简报")
            self.assertFalse(generated.ok)
            self.assertTrue(generated.result.ok)
            self.assertEqual(generated.result.text, self.content)
            self.assertIn("disk unavailable", generated.save_error)
            self.assertEqual(len(runtime.llm.calls), 1)
            self.assertFalse(runtime._busy)

    def test_missing_profile_and_unscripted_mock_do_not_archive_setup_questions(self):
        with Runtime.create(Settings(workdir=self.root), script=[self.content]) as runtime:
            generated = runtime.generate_brief()
            self.assertEqual(generated.result.stop_reason, "blocked")
            self.assertEqual(len(runtime.llm.calls), 0)
            runtime.update_profile(identity="工程师", preferences="AI 技术新闻")
            self.assertTrue(runtime.generate_brief().ok)
        with Runtime.create(self.settings) as runtime:
            self.assertFalse(runtime.generate_brief().ok)
            self.assertEqual(len(runtime.llm.calls), 0)

    def test_config_directory_precedence(self):
        env = self.root / "example.env"
        env.write_text("AGENT_PROVIDER=mock\nAGENT_BRIEFS_DIR=.agent/file-briefs\n", encoding="utf-8")
        with patch.dict(os.environ, {"AGENT_BRIEFS_DIR": ".agent/env-briefs"}, clear=True):
            self.assertEqual(load_settings(env, workdir=self.root).briefs_dir, self.root / ".agent/env-briefs")
            self.assertEqual(load_settings(env, workdir=self.root, briefs_dir=".agent/explicit").briefs_dir,
                             self.root / ".agent/explicit")

    def test_cli_generate_list_and_read_without_model_or_onboarding(self):
        args = ["--workdir", str(self.root), "--provider", "mock", "--no-memory", "--json"]
        with patch("agent.runtime.build_client", return_value=MockLLM(script=[self.content])), \
                patch("builtins.input", side_effect=AssertionError("unexpected input")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([*args, "--brief", "--brief-title", "自定义标题"]), 0)
        generated = json.loads(output.getvalue())
        self.assertEqual(generated["brief"]["title"], "自定义标题")
        self.assertEqual(generated["save_error"], "")
        with patch("agent.runtime.build_client", side_effect=AssertionError("unexpected client")), \
                patch("builtins.input", side_effect=AssertionError("unexpected input")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([*args, "--list-briefs"]), 0)
        records = json.loads(output.getvalue())
        self.assertEqual(records[0]["id"], generated["brief"]["id"])
        with patch("agent.runtime.build_client", side_effect=AssertionError("unexpected client")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([*args, "--read-brief", records[0]["id"]]), 0)
        self.assertEqual(json.loads(output.getvalue())["content"], self.content)

    def test_cli_archive_failure_returns_nonzero_and_preserves_json(self):
        args = ["--workdir", str(self.root), "--provider", "mock", "--no-memory", "--brief", "--json"]
        with patch("agent.runtime.build_client", return_value=MockLLM(script=[self.content])), \
                patch("agent.briefs.BriefStore.save", side_effect=OSError("disk full")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args), 1)
        generated = json.loads(output.getvalue())
        self.assertEqual(generated["result"]["text"], self.content)
        self.assertIn("disk full", generated["save_error"])
        self.assertIsNone(generated["brief"])

    def test_cli_interactive_archive_commands(self):
        with patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", side_effect=["/brief 关注开源模型", "/briefs", "/read-brief ../outside", "/reset", "/exit"]), \
                patch("agent.runtime.build_client", return_value=MockLLM(script=[self.content])), \
                contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["--workdir", str(self.root), "--provider", "mock", "--no-memory"]), 0)
        self.assertIn("今日 AI 新闻", output.getvalue())
        self.assertIn("已保存", errors.getvalue())
        self.assertIn("invalid brief id", errors.getvalue())
        self.assertEqual(len(self.store.list()), 1)


if __name__ == "__main__":
    unittest.main()
