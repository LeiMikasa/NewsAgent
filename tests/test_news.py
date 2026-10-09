import io
import json
import os
import tempfile
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from agent import Runtime, Settings, load_settings
from agent.events import Hooks, POST_TOOL_USE, PRE_TOOL_USE
from agent.tools import ToolRegistry
from agent.tools.news import _NoRedirect


def response(data):
    return io.BytesIO(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def article(url="https://example.com/news", content="AI 模型发布", **extra):
    return {"url": url, "title": "新模型", "content": content,
            "published_date": "2026-10-09T08:00:00Z", "score": 0.9, **extra}


class NewsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="newsagent-news-")
        self.root = Path(self.temporary.name)
        self.settings = Settings(workdir=self.root, memory_enabled=False,
                                 tavily_api_key="test-secret-key", tavily_timeout=12)
        self.runtime = Runtime.create(self.settings)
        self.opener = MagicMock()
        self.transport = patch("agent.tools.news.build_opener", return_value=self.opener)
        self.transport.start()

    def tearDown(self):
        self.transport.stop()
        self.runtime.close()
        self.temporary.cleanup()

    def serve(self, data):
        self.opener.open.return_value = response(data)

    def request_payload(self):
        request = self.opener.open.call_args.args[0]
        return request, json.loads(request.data)

    def test_default_runtime_has_seven_tools_and_custom_registry_is_preserved(self):
        self.assertEqual(len(self.runtime.tools.names()), 7)
        self.assertIn("search_news", self.runtime.tools.names())
        self.assertIn("fetch_article", self.runtime.tools.names())
        with Runtime.create(self.settings, tools=ToolRegistry()) as custom:
            self.assertEqual(custom.tools.names(), [])

    def test_missing_key_does_not_send_request(self):
        self.settings.tavily_api_key = None
        for name, args in [("search_news", {"query": "AI"}),
                           ("fetch_article", {"url": "https://example.com/news"})]:
            self.assertEqual(self.runtime.call_tool(name, **args).error_code, "MISSING_API_KEY")
        self.opener.open.assert_not_called()

    def test_search_payload_authentication_and_normalized_result(self):
        self.serve({"results": [article()], "usage": {"credits": 1}, "answer": "unused"})
        result = self.runtime.call_tool("search_news", query="  开源模型  ")
        self.assertTrue(result.ok, result.content)
        request, payload = self.request_payload()
        self.assertEqual(request.full_url, "https://api.tavily.com/search")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret-key")
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 12)
        self.assertEqual(payload["query"], "开源模型")
        self.assertEqual((payload["topic"], payload["time_range"], payload["max_results"]), ("news", "day", 10))
        self.assertTrue(payload["filter_by_published_date"])
        self.assertFalse(payload["include_answer"])
        self.assertFalse(payload["include_raw_content"])
        data = json.loads(result.content)
        self.assertEqual(data["returned_count"], 1)
        self.assertEqual(data["results"][0]["snippet"], "AI 模型发布")
        self.assertEqual(data["results"][0]["source"], "example.com")
        self.assertEqual(data["credits_used"], 1)
        self.assertNotIn("answer", data)
        self.assertNotIn("test-secret-key", result.content)

    def test_search_parameters_and_domain_normalization(self):
        self.serve({"results": []})
        result = self.runtime.call_tool("search_news", query="AI", time_range="week", limit=3,
                                        domains=["EXAMPLE.COM", "example.com."])
        self.assertTrue(result.ok)
        _, payload = self.request_payload()
        self.assertEqual(payload["include_domains"], ["example.com"])
        self.assertEqual((payload["time_range"], payload["max_results"]), ("week", 3))

    def test_schema_invalid_inputs_never_call_network(self):
        cases = [("search_news", {"query": " "}), ("search_news", {"query": "x" * 2001}),
                 ("search_news", {"query": "AI", "limit": 21}),
                 ("search_news", {"query": "AI", "limit": True}),
                 ("search_news", {"query": "AI", "time_range": "hour"}),
                 ("search_news", {"query": "AI", "domains": None}),
                 ("search_news", {"query": "AI", "typo": True}),
                 ("fetch_article", {}), ("fetch_article", {"url": "x", "max_chars": 0}),
                 ("fetch_article", {"url": "x", "extract_depth": "invalid"})]
        for name, args in cases:
            with self.subTest(name=name, args=args):
                self.assertEqual(self.runtime.call_tool(name, **args).error_code, "INVALID_ARGUMENT")
        self.opener.open.assert_not_called()

    def test_non_public_urls_and_domains_never_call_network(self):
        urls = ["file:///C:/secret", "ftp://example.com/x", "https://localhost/x",
                "http://127.0.0.1/x", "http://127.1/x", "http://10.0.0.1/x",
                "http://169.254.169.254/x", "http://[::1]/x", "https://host.local/x",
                "https://user:password@example.com/x", "https://example.com:8000/x",
                "https://example.com:notaport/x", "https://example.com/\nfoo", "https://example.com\\evil"]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.runtime.call_tool("fetch_article", url=url).error_code, "INVALID_ARGUMENT")
        for domain in ["https://example.com", "example.com/path", "localhost", "10.0.0.1"]:
            with self.subTest(domain=domain):
                self.assertEqual(self.runtime.call_tool("search_news", query="AI", domains=[domain]).error_code,
                                 "INVALID_ARGUMENT")
        self.opener.open.assert_not_called()

    def test_search_filters_bad_results_and_duplicate_urls(self):
        self.serve({"results": [article(), article(url="https://example.com/news#top"),
                                article(url="file:///x"), "bad", article(url="https://other.com/x", published_date=None)]})
        result = self.runtime.call_tool("search_news", query="AI")
        data = json.loads(result.content)
        self.assertEqual(data["returned_count"], 2)
        self.assertEqual(data["duplicates_removed"], 1)
        self.assertEqual(data["skipped_results"], 2)
        self.assertIsNone(data["results"][1]["published_date"])

    def test_search_output_remains_json_under_budget(self):
        self.settings.max_tool_output = 1400
        self.serve({"results": [article(url=f"https://example.com/{i}", content='中"\n' * 2000) for i in range(20)]})
        result = self.runtime.call_tool("search_news", query="AI", limit=20)
        self.assertTrue(result.ok)
        self.assertLessEqual(len(result.content), 1400)
        data = json.loads(result.content)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["returned_count"], len(data["results"]))
        self.assertGreater(data["results_omitted"], 0)

    def test_empty_search_is_success_but_malformed_response_is_error(self):
        self.serve({"results": []})
        result = self.runtime.call_tool("search_news", query="AI")
        self.assertTrue(result.ok)
        self.assertEqual(json.loads(result.content)["results"], [])
        self.serve({"unexpected": []})
        self.assertEqual(self.runtime.call_tool("search_news", query="AI").error_code, "INVALID_RESPONSE")

    def test_fetch_payload_and_markdown_content(self):
        self.serve({"results": [{"url": "https://example.com/news", "raw_content": "# 标题\n正文"}],
                    "failed_results": [], "usage": {"credits": 1}})
        result = self.runtime.call_tool("fetch_article", url="https://EXAMPLE.COM/news#section", extract_depth="advanced")
        self.assertTrue(result.ok)
        request, payload = self.request_payload()
        self.assertEqual(request.full_url, "https://api.tavily.com/extract")
        self.assertEqual(payload["urls"], ["https://example.com/news"])
        self.assertEqual(payload["format"], "markdown")
        self.assertEqual(payload["extract_depth"], "advanced")
        self.assertNotIn("query", payload)  # 不要求 rerank，获取正文而非相关片段。
        data = json.loads(result.content)
        self.assertEqual(data["content"], "# 标题\n正文")
        self.assertFalse(data["truncated"])
        self.assertEqual(data["returned_chars"], data["original_chars"])

    def test_fetch_truncation_respects_requested_chars_and_json_budget(self):
        content = '中"\n\\' * 4000
        self.serve({"results": [{"url": "https://example.com/news", "raw_content": content}]})
        result = self.runtime.call_tool("fetch_article", url="https://example.com/news", max_chars=30)
        data = json.loads(result.content)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["content"], content[:30])
        self.assertEqual(data["original_chars"], len(content))
        self.settings.max_tool_output = 700
        self.serve({"results": [{"url": "https://example.com/news", "raw_content": content}]})
        result = self.runtime.call_tool("fetch_article", url="https://example.com/news")
        self.assertTrue(result.ok)
        self.assertLessEqual(len(result.content), 700)
        data = json.loads(result.content)
        self.assertTrue(data["truncated"])
        self.assertGreater(data["returned_chars"], 0)
        self.assertEqual(data["returned_chars"], len(data["content"]))

    def test_extract_failure_empty_and_mismatched_pages(self):
        cases = [({"results": [], "failed_results": [{"error": "test-secret-key"}]}, "EXTRACTION_FAILED"),
                 ({"results": [{"url": "https://example.com/news", "raw_content": " "}]}, "EMPTY_CONTENT"),
                 ({"results": [{"url": "https://other.com/x", "raw_content": "wrong page"}]}, "EMPTY_CONTENT"),
                 ({"unexpected": []}, "INVALID_RESPONSE")]
        for data, code in cases:
            with self.subTest(code=code):
                self.serve(data)
                result = self.runtime.call_tool("fetch_article", url="https://example.com/news")
                self.assertEqual(result.error_code, code)
                self.assertNotIn("test-secret-key", result.content)

    def test_http_errors_are_classified_without_leaking_secrets_or_retrying(self):
        for status, code in [(400, "INVALID_REQUEST"), (401, "AUTH_ERROR"), (403, "AUTH_ERROR"),
                             (422, "INVALID_REQUEST"), (429, "RATE_LIMITED"), (432, "QUOTA_EXCEEDED"),
                             (433, "QUOTA_EXCEEDED"), (503, "UPSTREAM_ERROR"), (302, "UPSTREAM_ERROR")]:
            with self.subTest(status=status):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = HTTPError("https://api.tavily.com/search", status,
                                                        "test-secret-key", {}, io.BytesIO(b"test-secret-key"))
                result = self.runtime.call_tool("search_news", query="AI")
                self.assertEqual(result.error_code, code)
                self.assertNotIn("test-secret-key", result.content)
                self.assertEqual(self.opener.open.call_count, 1)

    def test_network_timeout_and_invalid_json(self):
        for error, code in [(TimeoutError("test-secret-key"), "TIMEOUT"),
                            (URLError(TimeoutError()), "TIMEOUT"),
                            (URLError("test-secret-key"), "NETWORK_ERROR"),
                            (IncompleteRead(b"test-secret-key"), "NETWORK_ERROR"),
                            (OSError("test-secret-key"), "NETWORK_ERROR")]:
            self.opener.open.side_effect = error
            result = self.runtime.call_tool("search_news", query="AI")
            self.assertEqual(result.error_code, code)
            self.assertNotIn("test-secret-key", result.content)
        self.opener.open.side_effect = None
        for raw in [b"not json", b"[]", b"\xff"]:
            self.opener.open.return_value = io.BytesIO(raw)
            self.assertEqual(self.runtime.call_tool("search_news", query="AI").error_code, "INVALID_RESPONSE")

    def test_response_size_and_output_limits(self):
        self.opener.open.return_value = io.BytesIO(b"x" * 21)
        with patch("agent.tools.news.MAX_RESPONSE_BYTES", 20):
            self.assertEqual(self.runtime.call_tool("search_news", query="AI").error_code, "RESPONSE_TOO_LARGE")
        self.settings.max_tool_output = 200
        for name, args, body in [("search_news", {"query": "AI"}, {"results": []}),
                                  ("fetch_article", {"url": "https://example.com/news"},
                                   {"results": [{"url": "https://example.com/news", "raw_content": "text"}]})]:
            self.serve(body)
            self.assertEqual(self.runtime.call_tool(name, **args).error_code, "OUTPUT_LIMIT")

    def test_nan_fields_are_not_returned_as_invalid_json(self):
        self.serve({"results": [article(score=float("nan"))], "usage": {"credits": float("inf")}})
        result = self.runtime.call_tool("search_news", query="AI")
        data = json.loads(result.content)
        self.assertIsNone(data["results"][0]["score"])
        self.assertIsNone(data["credits_used"])

    def test_search_fetch_model_loop_and_hooks(self):
        self.opener.open.side_effect = [response({"results": [article()]}),
                                       response({"results": [{"url": "https://example.com/news", "raw_content": "事实正文"}]})]
        events, hooks = [], Hooks()
        hooks.register(PRE_TOOL_USE, lambda block, ctx: events.append(("pre", block["name"])))
        hooks.register(POST_TOOL_USE, lambda block, result, ctx: events.append(("post", block["name"])))
        script = [{"tool": "search_news", "input": {"query": "AI"}},
                  {"tool": "fetch_article", "input": {"url": "https://example.com/news"}}, "完成简报"]
        with Runtime.create(self.settings, script=script, hooks=hooks) as runtime:
            result = runtime.submit("生成今日 AI 简报")
            self.assertTrue(result.ok)
            self.assertEqual((result.turns, result.tool_calls), (3, 2))
            self.assertEqual(events, [("pre", "search_news"), ("post", "search_news"),
                                      ("pre", "fetch_article"), ("post", "fetch_article")])
            output = runtime.llm.calls[2]["messages"][-1]["content"][0]
            self.assertFalse(output["is_error"])
            self.assertEqual(json.loads(output["content"])["content"], "事实正文")

    def test_hook_can_block_network_tool(self):
        self.runtime.hooks.register(PRE_TOOL_USE, lambda block, ctx: "blocked by policy")
        self.assertEqual(self.runtime.call_tool("search_news", query="AI").error_code, "POLICY_BLOCKED")
        self.opener.open.assert_not_called()

    def test_redirects_are_disabled(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "moved", {}, "https://other.com"))

    def test_env_precedence_validation_and_key_not_in_repr(self):
        env = self.root / ".env"
        env.write_text("TAVILY_API_KEY=file-key\nTAVILY_TIMEOUT=15\n", encoding="utf-8")
        with patch.dict(os.environ, {"TAVILY_API_KEY": "environment-key"}, clear=True):
            settings = load_settings(env, workdir=self.root)
            self.assertEqual(settings.tavily_api_key, "environment-key")
            self.assertEqual(settings.tavily_timeout, 15)
            self.assertNotIn("environment-key", repr(settings))
            self.assertEqual(load_settings(env, workdir=self.root, tavily_timeout=9).tavily_timeout, 9)
        for value in [0, 121, True, float("nan"), float("inf"), "30"]:
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                Settings(workdir=self.root, tavily_timeout=value)
        with self.assertRaises(ValueError):
            Settings(workdir=self.root, tavily_api_key="bad\nkey")


if __name__ == "__main__":
    unittest.main()
