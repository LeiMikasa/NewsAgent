import json
import os
import smtplib
import sqlite3
import ssl
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent import Runtime, Settings, load_settings
from agent.briefs import SHANGHAI
from agent.llm import LLMResponse
from backend.app import create_app
from backend.email_delivery import EmailSender
from backend.storage import StateStore


class EmailDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="newsagent-email-")
        self.root = Path(self.temp.name)
        self.settings = Settings(workdir=self.root, email_enabled=True, smtp_username="12345@qq.com",
                                 smtp_password="offline-authorization-code")
        self.content = "# 今日 AI 新闻\n\n模拟报道：[来源](https://example.com/news)。\n"
        self.sender = EmailSender(self.settings)
        self.brief = {"id": "2026-10-10_080000_aaaaaaaa", "title": "今日 AI 新闻",
                      "created_at": "2026-10-10T08:00:00+08:00", "content": self.content}
        self.runtimes = []

    def tearDown(self):
        self.temp.cleanup()

    def factory(self, on_event):
        runtime = Runtime.create(self.settings, script=[self.content], on_event=on_event)
        self.runtimes.append(runtime)
        return runtime

    def app(self, factory=None):
        return create_app(self.settings, runtime_factory=factory or self.factory)

    def set_profile(self, client):
        response = client.put("/api/profile", json={"identity": "工程师", "preferences": "AI 技术新闻"})
        self.assertEqual(response.status_code, 200)

    def due(self):
        return datetime.now(SHANGHAI).replace(hour=8, minute=0, second=0, microsecond=0)

    def wait_run(self, service, run_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = service.store.get_run(run_id)
            if not service.status()["busy"] and run["status"] not in ("queued", "running"):
                return run
            time.sleep(0.01)
        self.fail("email run did not finish")

    def test_smtp_ssl_message_utf8_attachment_and_recipient(self):
        self.settings.email_to = "67890@qq.com"
        message_id = self.sender.message_id("test-run")
        with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp:
            smtp.return_value.send_message.return_value = {}
            result = self.sender.send(self.brief, message_id)
            self.assertEqual(result.status, "sent")
            self.assertEqual(smtp.call_args.args, ("smtp.qq.com", 465))
            self.assertEqual(smtp.call_args.kwargs["timeout"], 30)
            context = smtp.call_args.kwargs["context"]
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            client = smtp.return_value
            client.login.assert_called_once_with("12345@qq.com", "offline-authorization-code")
            message = client.send_message.call_args.args[0]
            self.assertEqual(message["To"], "67890@qq.com")
            self.assertEqual(message["From"], "12345@qq.com")
            self.assertEqual(message["Message-ID"], message_id)
            self.assertIn("今日 AI 新闻", message["Subject"])
            self.assertIn(self.content, message.get_body(preferencelist=("plain",)).get_content())
            attachment = list(message.iter_attachments())[0]
            self.assertEqual(attachment.get_filename(), self.brief["id"] + ".md")
            self.assertEqual(attachment.get_payload(decode=True).decode("utf-8"), self.content)
            self.assertEqual(client.send_message.call_args.kwargs["to_addrs"], ["67890@qq.com"])
            client.send_message.assert_called_once()
            client.quit.assert_called_once()
            client.close.assert_called_once()

    def test_disabled_and_missing_configuration_do_not_connect(self):
        self.settings.email_enabled = False
        with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp:
            self.assertEqual(self.sender.send(self.brief, "<test@qq.com>").status, "failed")
            smtp.assert_not_called()
        self.settings.email_enabled = True
        self.settings.smtp_password = None
        with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp:
            result = self.sender.send(self.brief, "<test@qq.com>")
            self.assertEqual(result.status, "failed")
            self.assertIn("SMTP_PASSWORD", result.error)
            smtp.assert_not_called()

    def test_authentication_and_explicit_smtp_rejection_are_failed_without_secret_echo(self):
        failures = [smtplib.SMTPAuthenticationError(535, b"offline-authorization-code"),
                    smtplib.SMTPRecipientsRefused({"12345@qq.com": (550, b"rejected")}),
                    smtplib.SMTPSenderRefused(550, b"rejected", "12345@qq.com"),
                    smtplib.SMTPDataError(552, b"rejected"), smtplib.SMTPNotSupportedError("no auth")]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp:
                client = smtp.return_value
                if isinstance(failure, smtplib.SMTPAuthenticationError):
                    client.login.side_effect = failure
                else:
                    client.send_message.side_effect = failure
                result = self.sender.send(self.brief, "<test@qq.com>")
                self.assertEqual(result.status, "failed")
                self.assertNotIn("offline-authorization-code", result.error)
                self.assertLessEqual(client.send_message.call_count, 1)

    def test_network_failures_before_submission_are_failed_after_submission_are_unknown(self):
        for failure in (TimeoutError("timeout"), smtplib.SMTPServerDisconnected("connection lost")):
            with self.subTest(failure=type(failure).__name__), patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp:
                smtp.side_effect = failure
                self.assertEqual(self.sender.send(self.brief, "<test@qq.com>").status, "failed")
            with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp:
                smtp.return_value.login.side_effect = failure
                self.assertEqual(self.sender.send(self.brief, "<test@qq.com>").status, "failed")
                smtp.return_value.send_message.assert_not_called()
            with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp:
                smtp.return_value.send_message.side_effect = failure
                self.assertEqual(self.sender.send(self.brief, "<test@qq.com>").status, "unknown")
                smtp.return_value.send_message.assert_called_once()

    def test_quit_and_close_errors_after_acceptance_do_not_change_sent_result(self):
        with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp:
            client = smtp.return_value
            client.send_message.return_value = {}
            client.quit.side_effect = smtplib.SMTPServerDisconnected("QUIT failed")
            client.close.side_effect = OSError("close failed")
            self.assertEqual(self.sender.send(self.brief, "<test@qq.com>").status, "sent")
            client.send_message.assert_called_once()

    def test_settings_validation_precedence_and_password_not_in_repr(self):
        self.assertNotIn("offline-authorization-code", repr(self.settings))
        cases = ({"email_enabled": "true"}, {"smtp_port": 0}, {"smtp_port": True}, {"smtp_timeout": 0},
                 {"smtp_host": "smtp.qq.com\n"}, {"smtp_password": "a\nb"},
                 {"smtp_username": "name@example.com,other@example.com"}, {"email_to": "bad-address"})
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                Settings(workdir=self.root, **values)
        env = self.root / "test.env"
        env.write_text("EMAIL_ENABLED=true\nSMTP_USERNAME=12345@qq.com\nSMTP_PASSWORD=file-code\n"
                       "EMAIL_TO=67890@qq.com\nSMTP_TIMEOUT=15\n", encoding="utf-8")
        with patch.dict(os.environ, {"SMTP_PASSWORD": "environment-code"}, clear=True):
            settings = load_settings(env, workdir=self.root)
            self.assertTrue(settings.email_enabled)
            self.assertEqual(settings.smtp_password, "environment-code")
            self.assertEqual(settings.email_to, "67890@qq.com")
            self.assertEqual(settings.smtp_timeout, 15)
            self.assertEqual(load_settings(env, workdir=self.root, smtp_password="explicit-code").smtp_password,
                             "explicit-code")

    def test_scheduled_success_sends_saved_brief_once_and_exposes_delivery_state(self):
        app = self.app()
        with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp, TestClient(app) as client:
            smtp.return_value.send_message.return_value = {}
            self.set_profile(client)
            client.put("/api/subscription", json={"enabled": True, "time": "08:00"})
            run = app.state.service.run_scheduled(now=self.due())
            finished = self.wait_run(app.state.service, run["id"])
            self.assertEqual(finished["status"], "succeeded")
            self.assertEqual(finished["email_status"], "sent")
            self.assertEqual(finished["email_attempts"], 1)
            self.assertIsNotNone(finished["email_sent_at"])
            self.assertEqual(finished["email_recipient"], "12345@qq.com")
            message = smtp.return_value.send_message.call_args.args[0]
            self.assertIn(finished["brief_id"], message.get_body(preferencelist=("plain",)).get_content())
            self.assertEqual(message["Message-ID"], finished["email_message_id"])
            self.assertEqual(app.state.service.run_scheduled(now=self.due())["status"], "already_attempted")
            self.assertEqual(client.post("/api/runs/" + run["id"] + "/send-email").status_code, 422)
            smtp.return_value.send_message.assert_called_once()
            self.assertEqual(client.get("/api/runs").json()[0]["email_status"], "sent")
            self.assertEqual(client.get("/api/email").json()["recipient"], "12345@qq.com")
            self.assertNotIn("offline-authorization-code", client.get("/api/email").text)

    def test_manual_generation_does_not_send_until_explicit_send_request(self):
        app = self.app()
        with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp, TestClient(app) as client:
            smtp.return_value.send_message.return_value = {}
            self.set_profile(client)
            run = client.post("/api/generate", json={}).json()
            self.assertEqual(self.wait_run(app.state.service, run["id"])["email_status"], "not_requested")
            smtp.assert_not_called()
            response = client.post("/api/runs/" + run["id"] + "/send-email")
            self.assertEqual(response.status_code, 202)
            self.assertEqual(self.wait_run(app.state.service, run["id"])["email_status"], "sent")
            self.assertEqual(len(self.runtimes), 1)
            smtp.return_value.send_message.assert_called_once()

    def test_email_failure_keeps_successful_brief_and_retry_does_not_call_model(self):
        app = self.app()
        with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp, TestClient(app) as client:
            smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad auth")
            self.set_profile(client)
            client.put("/api/subscription", json={"enabled": True, "time": "08:00"})
            run = app.state.service.run_scheduled(now=self.due())
            failed = self.wait_run(app.state.service, run["id"])
            self.assertEqual(failed["status"], "succeeded")
            self.assertEqual(failed["email_status"], "failed")
            self.assertIn("认证失败", failed["email_error"])
            brief_id = failed["brief_id"]
            self.assertEqual(client.get("/api/briefs/" + brief_id).json()["content"], self.content)
            smtp.return_value.login.side_effect = None
            smtp.return_value.send_message.return_value = {}
            self.assertEqual(client.post("/api/runs/" + run["id"] + "/send-email").status_code, 202)
            retried = self.wait_run(app.state.service, run["id"])
            self.assertEqual(retried["email_status"], "sent")
            self.assertEqual(retried["email_attempts"], 2)
            self.assertEqual(retried["brief_id"], brief_id)
            self.assertEqual(retried["email_message_id"], failed["email_message_id"])
            self.assertEqual(len(self.runtimes), 1)
            self.assertEqual(len(self.runtimes[0].llm.calls), 1)

    def test_failed_generation_never_sends_mail(self):
        def factory(on_event):
            return Runtime.create(self.settings, script=[LLMResponse([])] * 3, on_event=on_event)
        app = self.app(factory)
        with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp, TestClient(app) as client:
            self.set_profile(client)
            client.put("/api/subscription", json={"enabled": True, "time": "08:00"})
            run = app.state.service.run_scheduled(now=self.due())
            failed = self.wait_run(app.state.service, run["id"])
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["email_status"], "not_requested")
            self.assertEqual(client.post("/api/runs/" + run["id"] + "/send-email").status_code, 422)
            smtp.assert_not_called()

    def test_unknown_send_is_not_automatically_replayed_on_restart(self):
        app = self.app()
        with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp, TestClient(app) as client:
            smtp.return_value.send_message.side_effect = TimeoutError("delivery uncertain")
            self.set_profile(client)
            client.put("/api/subscription", json={"enabled": True, "time": "08:00"})
            run = app.state.service.run_scheduled(now=self.due())
            finished = self.wait_run(app.state.service, run["id"])
            self.assertEqual(finished["email_status"], "unknown")
            self.assertEqual(finished["status"], "succeeded")
            smtp.return_value.send_message.assert_called_once()
        restarted = self.app()
        with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp, TestClient(restarted) as client:
            self.assertEqual(client.get("/api/runs/" + run["id"]).json()["email_status"], "unknown")
            self.assertEqual(restarted.state.service.run_scheduled(now=self.due())["status"], "already_attempted")
            smtp.assert_not_called()
            self.assertEqual(len(self.runtimes), 1)

    def test_pending_or_sending_on_restart_marked_unknown_generation_remains_successful(self):
        app = self.app()
        with TestClient(app) as client:
            self.set_profile(client)
            run = client.post("/api/generate", json={}).json()
            self.wait_run(app.state.service, run["id"])
            app.state.service.store.queue_email(run["id"])
            app.state.service.store.start_email(run["id"], "<persisted@qq.com>", "12345@qq.com")
        with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp, TestClient(self.app()) as client:
            recovered = client.get("/api/runs/" + run["id"]).json()
            self.assertEqual(recovered["status"], "succeeded")
            self.assertEqual(recovered["email_status"], "unknown")
            self.assertIn("上次邮件发送中断", recovered["email_error"])
            self.assertEqual(recovered["email_message_id"], "<persisted@qq.com>")
            smtp.assert_not_called()

    def test_schema_v1_migrates_without_losing_history(self):
        directory = self.root / ".agent"
        directory.mkdir()
        with closing(sqlite3.connect(directory / "backend.sqlite3")) as db:
            db.execute("CREATE TABLE subscription(id INTEGER PRIMARY KEY,enabled INTEGER,time TEXT,timezone TEXT,updated_at TEXT)")
            db.execute("INSERT INTO subscription VALUES (1,0,'09:00','Asia/Shanghai','old-time')")
            db.execute("""CREATE TABLE runs(id TEXT PRIMARY KEY,source TEXT,scheduled_date TEXT,status TEXT,
                created_at TEXT,started_at TEXT,finished_at TEXT,progress TEXT,brief_id TEXT,error TEXT,result TEXT)""")
            db.execute("INSERT INTO runs VALUES ('old-run','manual',NULL,'failed','old-time',NULL,NULL,'',NULL,'old-error',NULL)")
            db.execute("PRAGMA user_version=1")
            db.commit()
        store = StateStore(self.root)
        store.initialize()
        self.assertEqual(store.subscription()["time"], "09:00")
        old = store.get_run("old-run")
        self.assertEqual(old["error"], "old-error")
        self.assertEqual(old["email_status"], "not_requested")
        self.assertEqual(old["email_attempts"], 0)
        with store.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        store.initialize()

    def test_service_redacts_authorization_code_and_send_requires_ready_config(self):
        app = self.app()
        with TestClient(app) as client:
            self.assertNotIn(self.settings.smtp_password, app.state.service.clean_error(self.settings.smtp_password))
            self.set_profile(client)
            run = client.post("/api/generate", json={}).json()
            self.wait_run(app.state.service, run["id"])
            self.settings.email_enabled = False
            self.assertEqual(client.post("/api/runs/" + run["id"] + "/send-email").status_code, 422)
            self.settings.email_enabled = True
            self.settings.smtp_password = None
            response = client.post("/api/runs/" + run["id"] + "/send-email")
            self.assertEqual(response.status_code, 422)
            self.assertIn("SMTP_PASSWORD", response.text)
            self.assertFalse(client.get("/api/email").json()["configured"])

    def test_email_enabled_but_missing_auth_does_not_lose_generated_brief(self):
        self.settings.smtp_password = None
        app = self.app()
        with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp, TestClient(app) as client:
            self.set_profile(client)
            client.put("/api/subscription", json={"enabled": True, "time": "08:00"})
            run = app.state.service.run_scheduled(now=self.due())
            finished = self.wait_run(app.state.service, run["id"])
            self.assertEqual(finished["status"], "succeeded")
            self.assertEqual(finished["email_status"], "failed")
            self.assertIn("SMTP_PASSWORD", finished["email_error"])
            self.assertEqual(len(client.get("/api/briefs").json()), 1)
            smtp.assert_not_called()

    def test_failed_archive_does_not_send_mail(self):
        def factory(on_event):
            runtime = self.factory(on_event)
            def failed_save(*args, **kwargs):
                raise OSError("disk unavailable")
            runtime.briefs.save = failed_save
            return runtime
        app = self.app(factory)
        with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp, TestClient(app) as client:
            self.set_profile(client)
            client.put("/api/subscription", json={"enabled": True, "time": "08:00"})
            run = app.state.service.run_scheduled(now=self.due())
            finished = self.wait_run(app.state.service, run["id"])
            self.assertEqual(finished["status"], "failed")
            self.assertEqual(finished["email_status"], "not_requested")
            self.assertIn("disk unavailable", finished["error"])
            smtp.assert_not_called()

    def test_sending_email_keeps_backend_busy_and_rejects_duplicate_submission(self):
        entered, release = threading.Event(), threading.Event()
        app = self.app()
        with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp, TestClient(app) as client:
            def submitting(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test release timeout")
                return {}
            smtp.return_value.send_message.side_effect = submitting
            self.set_profile(client)
            client.put("/api/subscription", json={"enabled": True, "time": "08:00"})
            try:
                run = app.state.service.run_scheduled(now=self.due())
                self.assertTrue(entered.wait(2))
                status = client.get("/api/status").json()
                self.assertTrue(status["busy"])
                self.assertEqual(status["active_run"]["status"], "succeeded")
                self.assertEqual(status["active_run"]["email_status"], "sending")
                self.assertEqual(client.post("/api/generate", json={}).status_code, 409)
                self.assertEqual(client.post("/api/runs/" + run["id"] + "/send-email").status_code, 409)
                self.assertEqual(client.get("/api/briefs").status_code, 200)
            finally:
                release.set()
            self.assertEqual(self.wait_run(app.state.service, run["id"])["email_status"], "sent")
            smtp.return_value.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
