import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent import Runtime, Settings
from agent.briefs import SHANGHAI
from agent.llm import LLMResponse
from backend.app import create_app
from backend.scheduling import JOB_ID
from backend.service import BackendService


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="newsagent-backend-")
        self.root = Path(self.temp.name)
        self.settings = Settings(workdir=self.root)
        self.content = "# 今日 AI 新闻\n\n模拟新闻：[来源](https://example.com/news)。\n"
        self.runtimes = []

    def tearDown(self):
        self.temp.cleanup()

    def factory(self, on_event):
        runtime = Runtime.create(self.settings, script=[self.content], on_event=on_event)
        self.runtimes.append(runtime)
        return runtime

    def app(self, factory=None, settings=None):
        return create_app(settings or self.settings, runtime_factory=factory or self.factory)

    def set_profile(self, client):
        response = client.put("/api/profile", json={"identity": "AI 工程师", "preferences": "开源模型，中文简报"})
        self.assertEqual(response.status_code, 200, response.text)

    def wait_run(self, service, run_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = service.store.get_run(run_id)
            if run["status"] not in ("queued", "running") and not service.status()["busy"]:
                return run
            time.sleep(0.01)
        self.fail("background run did not finish")

    def due(self, minute=0):
        return datetime.now(SHANGHAI).replace(hour=8, minute=minute, second=0, microsecond=0)

    def test_profile_and_subscription_work_without_model_credentials(self):
        settings = Settings(workdir=self.root, provider="deepseek", model="offline-config-test")
        app = self.app(settings=settings)
        self.assertFalse((self.root / ".agent").exists())
        with patch("backend.service.Runtime.create", side_effect=AssertionError("unexpected model client")), \
                TestClient(app) as client:
            self.assertEqual(client.get("/api/profile").json()["missing_fields"], ["identity", "preferences"])
            self.assertFalse(client.get("/api/subscription").json()["enabled"])
            self.assertEqual(client.post("/api/generate", json={}).status_code, 409)
            self.assertEqual(client.put("/api/subscription", json={"enabled": True, "time": "08:00"}).status_code, 409)
            self.set_profile(client)
            self.assertEqual(client.get("/api/profile").json()["missing_fields"], [])
            self.assertIn("AI 工程师", (self.settings.memory_dir / "user-profile.md").read_text(encoding="utf-8"))
            response = client.put("/api/subscription", json={"enabled": True, "time": "08:30"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["enabled"])
            self.assertIsNotNone(response.json()["next_run_at"])
            scheduler = app.state.scheduler.scheduler
            self.assertEqual(len(scheduler.get_jobs()), 1)
            self.assertEqual(str(scheduler.get_job(JOB_ID).trigger.fields[5]), "8")
            client.put("/api/subscription", json={"enabled": True, "time": "09:45"})
            self.assertEqual(len(scheduler.get_jobs()), 1)
            self.assertEqual(str(scheduler.get_job(JOB_ID).trigger.fields[5]), "9")
            response = client.put("/api/subscription", json={"enabled": False, "time": "09:45"})
            self.assertIsNone(response.json()["next_run_at"])
            self.assertEqual(scheduler.get_jobs(), [])
            self.assertEqual(client.get("/api/briefs").json(), [])
            self.assertEqual(client.get("/api/status").json(), {"busy": False, "active_run": None})
            self.assertEqual(client.get("/docs").status_code, 200)
            schema = client.get("/openapi.json").json()
            self.assertIn("202", schema["paths"]["/api/generate"]["post"]["responses"])
            self.assertFalse(app.state.service.memory.llm.calls)

    def test_input_validation_has_no_state_changes(self):
        with TestClient(self.app()) as client:
            for body in ({"enabled": True, "time": "24:00"}, {"enabled": True, "time": "8:00"},
                         {"enabled": "true", "time": "08:00"}, {"enabled": True, "time": "08:00\n"},
                         {"enabled": True, "time": "08:00", "timezone": "UTC"},
                         {"enabled": False, "time": "08:00", "extra": 1}):
                self.assertEqual(client.put("/api/subscription", json=body).status_code, 422)
            for body in ({}, {"identity": " "}, {"identity": "x" * 4001}, {"identity": "a\x00b"},
                         {"identity": 1}, {"identity": "valid", "unexpected": True}):
                self.assertEqual(client.put("/api/profile", json=body).status_code, 422)
            for body in ({"prompt": " "}, {"prompt": "x" * 4001}, {"title": "a\nb"}, {"prompt": 42}):
                self.assertEqual(client.post("/api/generate", json=body).status_code, 422)
            self.assertFalse(client.get("/api/subscription").json()["enabled"])
            self.assertEqual(client.get("/api/runs").json(), [])
            self.assertEqual(client.get("/api/briefs?limit=101").status_code, 422)
            self.assertEqual(client.get("/api/briefs?before=../outside").status_code, 422)
            self.assertEqual(client.get("/api/runs?limit=0").status_code, 422)
            self.assertEqual(client.get("/api/runs/unknown").status_code, 404)
            self.assertEqual(client.get("/api/briefs/2026-10-10_080000_aaaaaaaa").status_code, 404)
            self.assertEqual(client.get("/api/briefs/invalid").status_code, 422)
            self.assertFalse(self.runtimes)

    def test_generation_is_async_reuses_archive_and_fresh_runtime_per_run(self):
        app = self.app()
        with TestClient(app) as client:
            self.set_profile(client)
            response = client.post("/api/generate", json={"prompt": "生成今日简报", "title": "我的简报"})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["status"], "queued")
            run = self.wait_run(app.state.service, response.json()["id"])
            self.assertEqual(run["status"], "succeeded")
            self.assertEqual(run["result"]["turns"], 1)
            self.assertNotIn("text", run["result"])
            self.assertEqual(client.get("/api/runs/" + run["id"]).json(), run)
            brief = client.get("/api/briefs/" + run["brief_id"]).json()
            self.assertEqual(brief["content"], self.content)
            self.assertEqual(brief["title"], "我的简报")
            self.assertEqual(len(client.get("/api/briefs").json()), 1)
            second = client.post("/api/generate", json={}).json()
            self.assertEqual(self.wait_run(app.state.service, second["id"])["status"], "succeeded")
            self.assertEqual(len(self.runtimes), 2)
            self.assertIsNot(self.runtimes[0], self.runtimes[1])
            self.assertEqual(len(self.runtimes[1].llm.calls[0]["messages"]), 1)
            self.assertIn("开源模型", self.runtimes[1].llm.calls[0]["system"])
            self.assertTrue(all(runtime._closed for runtime in self.runtimes))
            self.assertEqual(len(client.get("/api/runs").json()), 2)
            self.assertEqual(client.get("/api/status").json(), {"busy": False, "active_run": None})

    def test_busy_rejects_manual_generation_and_profile_edit_but_status_remains_available(self):
        entered, release = threading.Event(), threading.Event()
        def factory(on_event):
            def responder(*args):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test release timeout")
                return self.content
            return Runtime.create(self.settings, responder=responder, on_event=on_event)
        app = self.app(factory)
        with TestClient(app) as client:
            self.set_profile(client)
            try:
                run = client.post("/api/generate", json={}).json()
                self.assertTrue(entered.wait(2))
                status = client.get("/api/status").json()
                self.assertTrue(status["busy"])
                self.assertEqual(status["active_run"]["status"], "running")
                self.assertIn("[model]", status["active_run"]["progress"])
                self.assertEqual(client.post("/api/generate", json={}).status_code, 409)
                self.assertEqual(client.put("/api/profile", json={"preferences": "修改"}).status_code, 409)
                self.assertEqual(client.get("/api/profile").json()["preferences"], "开源模型，中文简报")
                self.assertEqual(client.get("/api/briefs").status_code, 200)
                client.put("/api/subscription", json={"enabled": True, "time": "08:00"})
                skipped = app.state.service.run_scheduled(now=self.due())
                self.assertEqual(skipped["status"], "skipped")
                self.assertIn("任务", skipped["error"])
                self.assertEqual(client.put("/api/subscription", json={"enabled": False, "time": "08:00"}).status_code, 200)
            finally:
                release.set()
            self.assertEqual(self.wait_run(app.state.service, run["id"])["status"], "succeeded")

    def test_scheduled_runs_deduplicate_per_day_and_manual_runs_remain_allowed(self):
        app = self.app()
        with TestClient(app) as client:
            self.set_profile(client)
            client.put("/api/subscription", json={"enabled": True, "time": "08:00"})
            first = app.state.service.run_scheduled(now=self.due())
            self.assertEqual(first["source"], "scheduled")
            self.assertEqual(self.wait_run(app.state.service, first["id"])["status"], "succeeded")
            self.assertEqual(app.state.service.run_scheduled(now=self.due())["status"], "already_attempted")
            manual = client.post("/api/generate", json={}).json()
            self.assertEqual(self.wait_run(app.state.service, manual["id"])["status"], "succeeded")
            tomorrow = app.state.service.run_scheduled(now=self.due() + timedelta(days=1))
            self.assertEqual(self.wait_run(app.state.service, tomorrow["id"])["status"], "succeeded")
            self.assertEqual(len(self.runtimes), 3)
            client.put("/api/subscription", json={"enabled": False, "time": "08:00"})
            self.assertEqual(app.state.service.run_scheduled(now=self.due())["status"], "disabled")

    def test_catchup_only_today_within_hour_and_does_not_backfill(self):
        app = self.app()
        with TestClient(app) as client:
            self.set_profile(client)
            client.put("/api/subscription", json={"enabled": True, "time": "08:00"})
            service = app.state.service
            for now in (self.due() - timedelta(seconds=1), self.due(0) + timedelta(hours=1, seconds=1),
                        self.due().replace(hour=0)):
                self.assertIsNone(service.catch_up(now=now))
                self.assertEqual(service.run_scheduled(now=now)["status"], "outside_window")
            caught = service.catch_up(now=self.due(30))
            self.assertEqual(self.wait_run(service, caught["id"])["status"], "succeeded")
            self.assertEqual(service.catch_up(now=self.due(45))["status"], "already_attempted")
            self.assertEqual(len(self.runtimes), 1)

    def test_restart_restores_subscription_and_marks_unfinished_runs_interrupted(self):
        app = self.app()
        scheduled_time = (datetime.now(SHANGHAI) + timedelta(hours=2)).strftime("%H:%M")
        with TestClient(app) as client:
            self.set_profile(client)
            client.put("/api/subscription", json={"enabled": True, "time": scheduled_time})
            first = app.state.service.store.create_run("manual")
            second = app.state.service.store.create_run("manual")
            app.state.service.store.start_run(second["id"])
        restarted = self.app()
        with TestClient(restarted) as client:
            subscription = client.get("/api/subscription").json()
            self.assertTrue(subscription["enabled"])
            self.assertEqual(subscription["time"], scheduled_time)
            self.assertIsNotNone(subscription["next_run_at"])
            self.assertEqual(client.get("/api/profile").json()["identity"], "AI 工程师")
            for run in (first, second):
                recovered = client.get("/api/runs/" + run["id"]).json()
                self.assertEqual(recovered["status"], "interrupted")
                self.assertIsNotNone(recovered["finished_at"])
            self.assertFalse(client.get("/api/status").json()["busy"])
            self.assertEqual(len(restarted.state.scheduler.scheduler.get_jobs()), 1)
            self.assertFalse(self.runtimes)

    def test_restart_invokes_catchup(self):
        app = self.app()
        with TestClient(app) as client:
            self.set_profile(client)
            client.put("/api/subscription", json={"enabled": True, "time": "08:00"})
        restarted = self.app()
        now = self.due(30)
        with patch("backend.service.datetime") as clock:
            clock.now.return_value = now
            with TestClient(restarted) as client:
                runs = client.get("/api/runs").json()
                self.assertEqual(len(runs), 1)
                self.assertEqual(self.wait_run(restarted.state.service, runs[0]["id"])["status"], "succeeded")
        self.assertEqual(len(self.runtimes), 1)

    def test_second_backend_cannot_corrupt_first_instances_state(self):
        app = self.app()
        with TestClient(app):
            seed = app.state.service.store.create_run("manual")
            app.state.service.store.start_run(seed["id"])
            with self.assertRaisesRegex(RuntimeError, "another NewsAgent backend"):
                with TestClient(self.app()):
                    self.fail("second backend should not start")
            self.assertEqual(app.state.service.store.get_run(seed["id"])["status"], "running")
        # OS lock is released normally and the persisted stale row is now recoverable.
        with TestClient(self.app()) as client:
            self.assertEqual(client.get("/api/runs/" + seed["id"]).json()["status"], "interrupted")

    def test_failed_model_run_recorded_without_archive_or_automatic_whole_task_retry(self):
        def factory(on_event):
            runtime = Runtime.create(self.settings, script=[LLMResponse([])] * 3, on_event=on_event)
            self.runtimes.append(runtime)
            return runtime
        app = self.app(factory)
        with TestClient(app) as client:
            self.set_profile(client)
            client.put("/api/subscription", json={"enabled": True, "time": "08:00"})
            run = app.state.service.run_scheduled(now=self.due())
            failed = self.wait_run(app.state.service, run["id"])
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["result"]["stop_reason"], "incomplete")
            self.assertIsNone(failed["brief_id"])
            self.assertIn("recovery exhausted", failed["error"])
            self.assertEqual(client.get("/api/briefs").json(), [])
            self.assertEqual(app.state.service.run_scheduled(now=self.due())["status"], "already_attempted")
            self.assertEqual(len(self.runtimes), 1)
            self.assertEqual(len(self.runtimes[0].llm.calls), 3)

    def test_save_failure_preserves_content_for_recovery_without_regenerating(self):
        def factory(on_event):
            runtime = self.factory(on_event)
            runtime.briefs.save = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk unavailable"))
            return runtime
        app = self.app(factory)
        with TestClient(app) as client:
            self.set_profile(client)
            run = client.post("/api/generate", json={}).json()
            failed = self.wait_run(app.state.service, run["id"])
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["result"]["stop_reason"], "final")
            self.assertEqual(failed["result"]["unsaved_content"], self.content)
            self.assertIn("disk unavailable", failed["result"]["save_error"])
            self.assertEqual(len(self.runtimes[0].llm.calls), 1)
            self.assertEqual(client.get("/api/briefs").json(), [])
            self.assertIsNone(client.get("/api/runs").json()[0]["result"])
            self.assertEqual(client.get("/api/runs/" + run["id"]).json()["result"]["unsaved_content"], self.content)

    def test_worker_exception_redacts_credentials_and_releases_busy_state(self):
        settings = Settings(workdir=self.root, api_key="private-model-key", tavily_api_key="private-news-key")
        def factory(on_event):
            raise RuntimeError("private-model-key and private-news-key")
        app = self.app(factory, settings)
        with TestClient(app) as client:
            self.set_profile(client)
            with self.assertLogs("backend.service", level="ERROR") as logged:
                run = client.post("/api/generate", json={}).json()
                failed = self.wait_run(app.state.service, run["id"])
            self.assertEqual(failed["status"], "failed")
            encoded = json.dumps(failed) + "".join(logged.output)
            self.assertNotIn("private-model-key", encoded)
            self.assertNotIn("private-news-key", encoded)
            self.assertIn("[redacted]", encoded)
            self.assertFalse(client.get("/api/status").json()["busy"])

    def test_subscription_apply_failure_rolls_back_persisted_config(self):
        service = BackendService(self.settings, runtime_factory=self.factory)
        service.start()
        try:
            service.update_profile(identity="工程师", preferences="AI")
            applied = []
            def apply(subscription):
                applied.append(subscription["enabled"])
                if subscription["enabled"]:
                    raise ValueError("schedule failed")
            with self.assertRaisesRegex(ValueError, "schedule failed"):
                service.update_subscription({"enabled": True, "time": "08:30", "timezone": "Asia/Shanghai"}, apply)
            self.assertFalse(service.store.subscription()["enabled"])
            self.assertEqual(service.store.subscription()["time"], "08:00")
            self.assertEqual(applied, [True, False])
        finally:
            service.close()

    def test_frontend_cors_only_allows_configured_origins(self):
        with TestClient(self.app()) as client:
            headers = {"Origin": "http://localhost:5173", "Access-Control-Request-Method": "PUT",
                       "Access-Control-Request-Headers": "content-type"}
            response = client.options("/api/profile", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:5173")
            headers["Origin"] = "http://unconfigured.example"
            self.assertEqual(client.options("/api/profile", headers=headers).status_code, 400)

    def test_scheduler_really_dispatches_to_shared_generation_service(self):
        app = self.app()
        with TestClient(app) as client:
            self.set_profile(client)
            client.put("/api/subscription", json={"enabled": True, "time": "08:00"})
            app.state.scheduler.scheduler.add_job(app.state.service.run_scheduled,
                trigger="date", run_date=datetime.now(SHANGHAI) + timedelta(milliseconds=100),
                kwargs={"now": self.due(30)}, id=JOB_ID, replace_existing=True)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                runs = app.state.service.store.list_runs()
                if runs:
                    break
                time.sleep(0.01)
            self.assertTrue(runs, "scheduler did not dispatch")
            run = self.wait_run(app.state.service, runs[0]["id"])
            self.assertEqual(run["status"], "succeeded")
            self.assertEqual(run["source"], "scheduled")
            self.assertEqual(len(client.get("/api/briefs").json()), 1)

    def test_graceful_shutdown_waits_for_current_run_and_rejects_new_work(self):
        entered, release = threading.Event(), threading.Event()
        def factory(on_event):
            def responder(*args):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test release timeout")
                return self.content
            return Runtime.create(self.settings, responder=responder, on_event=on_event)
        service = BackendService(self.settings, runtime_factory=factory)
        service.start()
        closer = None
        try:
            service.update_profile(identity="工程师", preferences="AI 新闻")
            run = service.generate()
            self.assertTrue(entered.wait(2))
            closer = threading.Thread(target=service.close)
            closer.start()
            deadline = time.monotonic() + 2
            while not service._closing and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(service._closing)
            self.assertTrue(closer.is_alive())
            with self.assertRaisesRegex(RuntimeError, "stopping"):
                service.generate()
        finally:
            release.set()
            if closer is not None:
                closer.join(timeout=5)
            service.close()
        self.assertFalse(closer.is_alive())
        self.assertEqual(service.store.get_run(run["id"])["status"], "succeeded")
        restarted = BackendService(self.settings, runtime_factory=self.factory)
        restarted.start()
        restarted.close()


if __name__ == "__main__":
    unittest.main()
