"""SQLite stores operational state; memory and briefs remain Markdown files."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from agent.briefs import SHANGHAI


def timestamp() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


class InstanceLock:
    """OS lock for one backend per workspace; an old lock file is harmless."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self) -> None:
        if self.path.is_symlink() or self.path.is_junction():
            raise PermissionError("backend lock cannot be a link")
        handle = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("another NewsAgent backend is already using this workspace") from exc
        self.handle = handle

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class StateStore:
    def __init__(self, workdir: Path):
        self.workdir = Path(workdir).resolve()
        self.path = self.workdir / ".agent/backend.sqlite3"

    def prepare(self) -> None:
        self._check_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._check_path()

    def _check_path(self) -> None:
        if (not self.path.resolve().is_relative_to(self.workdir) or self.path.is_symlink()
                or self.path.is_junction()):
            raise PermissionError("backend state must stay inside the workspace")

    @contextmanager
    def connection(self):
        self._check_path()
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("unsupported backend database version")
            if version == 0 and db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                raise ValueError("backend database contains unrelated tables")
            db.execute("""CREATE TABLE IF NOT EXISTS subscription (
                id INTEGER PRIMARY KEY CHECK (id=1), enabled INTEGER NOT NULL,
                time TEXT NOT NULL, timezone TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, scheduled_date TEXT,
                status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
                finished_at TEXT, progress TEXT NOT NULL DEFAULT '',
                brief_id TEXT, error TEXT NOT NULL DEFAULT '', result TEXT,
                email_status TEXT NOT NULL DEFAULT 'not_requested', email_attempts INTEGER NOT NULL DEFAULT 0,
                email_error TEXT NOT NULL DEFAULT '', email_sent_at TEXT,
                email_message_id TEXT, email_recipient TEXT)""")
            if version == 1:
                for definition in ("email_status TEXT NOT NULL DEFAULT 'not_requested'",
                                   "email_attempts INTEGER NOT NULL DEFAULT 0", "email_error TEXT NOT NULL DEFAULT ''",
                                   "email_sent_at TEXT", "email_message_id TEXT", "email_recipient TEXT"):
                    db.execute("ALTER TABLE runs ADD COLUMN " + definition)
            db.execute("CREATE INDEX IF NOT EXISTS runs_created ON runs(created_at DESC)")
            # Scheduled runs have one attempt per local date; manual runs are unrestricted.
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS scheduled_date_once
                ON runs(scheduled_date) WHERE source='scheduled' AND status!='skipped'""")
            db.execute("INSERT OR IGNORE INTO subscription VALUES (1, 0, '08:00', 'Asia/Shanghai', ?)",
                       (timestamp(),))
            db.execute("PRAGMA user_version=2")

    def recover_interrupted(self) -> None:
        with self.connection() as db:
            db.execute("""UPDATE runs SET status='interrupted', finished_at=?,
                error='后端上次运行中断，请查看简报历史后决定是否手动重新生成。'
                WHERE status IN ('queued', 'running')""", (timestamp(),))
            db.execute("""UPDATE runs SET email_status='unknown',
                email_error='上次邮件发送中断，结果不确定；请先检查邮箱再决定是否重发。'
                WHERE email_status IN ('pending', 'sending')""")

    def subscription(self) -> dict:
        with self.connection() as db:
            row = dict(db.execute("SELECT enabled,time,timezone,updated_at FROM subscription WHERE id=1").fetchone())
        row["enabled"] = bool(row["enabled"])
        return row

    def set_subscription(self, *, enabled: bool, time: str, timezone: str) -> dict:
        with self.connection() as db:
            db.execute("UPDATE subscription SET enabled=?,time=?,timezone=?,updated_at=? WHERE id=1",
                       (int(enabled), time, timezone, timestamp()))
        return self.subscription()

    def has_scheduled_run(self, day: str) -> bool:
        with self.connection() as db:
            return db.execute("SELECT 1 FROM runs WHERE source='scheduled' AND scheduled_date=? AND status!='skipped'",
                              (day,)).fetchone() is not None

    def create_run(self, source: str, *, day: str | None = None, status: str = "queued", error: str = "") -> dict:
        run_id = uuid.uuid4().hex
        with self.connection() as db:
            db.execute("""INSERT INTO runs(id,source,scheduled_date,status,created_at,finished_at,error)
                VALUES (?,?,?,?,?,?,?)""", (run_id, source, day, status, timestamp(),
                                          timestamp() if status == "skipped" else None, error))
        return self.get_run(run_id)

    def start_run(self, run_id: str) -> None:
        with self.connection() as db:
            db.execute("UPDATE runs SET status='running',started_at=? WHERE id=?", (timestamp(), run_id))

    def progress(self, run_id: str, message: str) -> None:
        with self.connection() as db:
            db.execute("UPDATE runs SET progress=? WHERE id=? AND status='running'", (message, run_id))

    def finish_run(self, run_id: str, *, ok: bool, brief_id: str | None, error: str, result: dict | None,
                   email_pending: bool = False) -> None:
        with self.connection() as db:
            db.execute("""UPDATE runs SET status=?,finished_at=?,brief_id=?,error=?,result=?,email_status=? WHERE id=?""",
                       ("succeeded" if ok else "failed", timestamp(), brief_id, error,
                        json.dumps(result, ensure_ascii=False) if result is not None else None,
                        "pending" if email_pending else "not_requested", run_id))

    def queue_email(self, run_id: str) -> dict:
        with self.connection() as db:
            db.execute("UPDATE runs SET email_status='pending',email_error='' WHERE id=?", (run_id,))
        return self.get_run(run_id)

    def start_email(self, run_id: str, message_id: str, recipient: str | None) -> None:
        with self.connection() as db:
            db.execute("""UPDATE runs SET email_status='sending',email_attempts=email_attempts+1,
                email_message_id=?,email_recipient=?,email_error='' WHERE id=?""", (message_id, recipient, run_id))

    def finish_email(self, run_id: str, status: str, error: str = "") -> None:
        with self.connection() as db:
            db.execute("UPDATE runs SET email_status=?,email_error=?,email_sent_at=? WHERE id=?",
                       (status, error, timestamp() if status == "sent" else None, run_id))

    @staticmethod
    def _run(row) -> dict:
        result = dict(row)
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def get_run(self, run_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run(row)

    def list_runs(self, limit: int = 20) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("run history limit must be an integer between 1 and 100")
        with self.connection() as db:
            # Failed-save bodies are potentially large; only a detail request loads that result.
            rows = db.execute("""SELECT id,source,scheduled_date,status,created_at,started_at,finished_at,
                progress,brief_id,error,NULL AS result,email_status,email_attempts,email_error,email_sent_at,
                email_message_id,email_recipient FROM runs ORDER BY rowid DESC LIMIT ?""", (limit,)).fetchall()
        return [self._run(row) for row in rows]
