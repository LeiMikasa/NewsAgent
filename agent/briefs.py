"""Local Markdown brief archive; one atomic file contains metadata and body."""

from __future__ import annotations

import heapq
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai") # 定义UTC+8:00时区，供保存时间使用
# 定义合法简报编号的格式  2026-10-10_080000_a1b2c3d4
ID_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}_[a-f0-9]{8}\Z", re.ASCII)
# 元信息头文件字节限制
HEADER_LIMIT = 8192
# 标题最多200个字符
TITLE_LIMIT = 200


class BriefStore:
    def __init__(self, workdir: Path, directory: Path | None = None, *, max_content_bytes: int = 2_000_000):
        self.workdir = Path(workdir).resolve()
        candidate = Path(directory) if directory is not None else Path(".agent/briefs")
        self.directory = candidate if candidate.is_absolute() else self.workdir / candidate
        if isinstance(max_content_bytes, bool) or not isinstance(max_content_bytes, int) or max_content_bytes <= 0:
            raise ValueError("max_content_bytes must be a positive integer")
        self.max_content_bytes = max_content_bytes
        self._directory()
    # 检查归档目录是否合规
    def _directory(self) -> Path:
        directory = self.directory.resolve()
        if not directory.is_relative_to(self.workdir) or directory == self.workdir:
            raise PermissionError("brief directory must stay inside the workspace")
        if directory.exists() and not directory.is_dir():
            raise NotADirectoryError(str(directory))
        return directory
    # 由编号得到安全文件路径
    def _path(self, brief_id: str) -> Path:
        # 要求整个字符串完全符合编号格式
        if not isinstance(brief_id, str) or not ID_PATTERN.fullmatch(brief_id):
            raise ValueError("invalid brief id; use an id returned by the history list")
        directory = self._directory()
        path = directory / f"{brief_id}.md"
        if path.is_symlink() or path.is_junction() or path.resolve().parent != directory:
            raise PermissionError("brief files cannot be links or escape the archive")
        return path

    @staticmethod
    def _title(content: str, title: str | None, now: datetime) -> str:
        if title is None:
            heading = next((line.lstrip("#").strip() for line in content.splitlines()
                            if re.match(r"^#{1,6}\s+\S", line)), "")
            title = heading[:TITLE_LIMIT] or f"AI 新闻简报 {now:%Y-%m-%d}"
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > TITLE_LIMIT:
            raise ValueError(f"brief title must contain 1..{TITLE_LIMIT} characters")
        if any(ord(char) < 32 for char in title):
            raise ValueError("brief title must be a single line without control characters")
        return title.strip()
    # 输入检查
    def save(self, content: str, *, title: str | None = None, provider: str = "", model: str = "",
             turns: int = 0, tool_calls: int = 0, tool_attempts: int = 0) -> dict:
        if not isinstance(content, str) or not content.strip() or "\x00" in content:
            raise ValueError("brief content must be nonempty text without NUL characters")
        body = content.encode("utf-8")
        if len(body) > self.max_content_bytes:
            raise ValueError(f"brief exceeds {self.max_content_bytes} bytes; content was not truncated")
        now = datetime.now(SHANGHAI)
        title = self._title(content, title, now)
        for value in (provider, model):
            if not isinstance(value, str) or len(value) > 500 or any(ord(char) < 32 for char in value):
                raise ValueError("provider/model must be single-line strings of at most 500 characters")
        for value in (turns, tool_calls, tool_attempts):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("brief counters must be nonnegative integers")
        directory = self._directory()
        directory.mkdir(parents=True, exist_ok=True)
        directory = self._directory()
        for _ in range(5):
            brief_id = f"{now:%Y-%m-%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
            metadata = {"schema_version": 1, "id": brief_id, "title": title,
                        "created_at": now.isoformat(timespec="seconds"), "timezone": "Asia/Shanghai",
                        "provider": provider, "model": model, "turns": turns,
                        "tool_calls": tool_calls, "tool_attempts": tool_attempts, "content_bytes": len(body)}
            header = ("---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n").encode("utf-8")
            if len(header) > HEADER_LIMIT:
                raise ValueError("brief metadata exceeds header limit")
            path = self._path(brief_id)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=directory, prefix=".brief-", delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(header)
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                # Publish the complete file without replacing an existing brief.
                os.link(temporary, path)
            except FileExistsError:
                continue
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return self._public_metadata(metadata, path)
        raise FileExistsError("could not allocate a unique brief id")

    def _public_metadata(self, metadata: dict, path: Path) -> dict:
        return {**metadata, "path": path.relative_to(self.workdir).as_posix()}

    def _read_metadata(self, handle, brief_id: str) -> dict:
        first = handle.readline(HEADER_LIMIT + 1)
        if first.rstrip(b"\r\n") != b"---":
            raise ValueError("brief metadata header is missing")
        size, lines = len(first), []
        while size < HEADER_LIMIT:
            line = handle.readline(HEADER_LIMIT - size + 1)
            size += len(line)
            if not line or size > HEADER_LIMIT:
                break
            if line.rstrip(b"\r\n") == b"---":
                try:
                    metadata = yaml.safe_load(b"".join(lines).decode("utf-8"))
                except (yaml.YAMLError, UnicodeError) as exc:
                    raise ValueError("invalid brief metadata") from exc
                self._validate_metadata(metadata, brief_id)
                return metadata
            lines.append(line)
        raise ValueError("brief metadata header is incomplete or too large")

    def _validate_metadata(self, metadata, brief_id: str) -> None:
        if not isinstance(metadata, dict) or type(metadata.get("schema_version")) is not int or metadata["schema_version"] != 1:
            raise ValueError("unsupported brief metadata")
        if metadata.get("id") != brief_id or metadata.get("timezone") != "Asia/Shanghai":
            raise ValueError("brief metadata does not match its file")
        try:
            created = datetime.fromisoformat(metadata["created_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid brief creation time") from exc
        if created.utcoffset() != timedelta(hours=8) or created.strftime("%Y-%m-%d_%H%M%S") != brief_id[:17]:
            raise ValueError("brief creation time does not match its id")
        self._title("", metadata.get("title"), created)
        if not isinstance(metadata.get("title"), str):
            raise ValueError("brief title is missing")
        for field in ("turns", "tool_calls", "tool_attempts", "content_bytes"):
            if type(metadata.get(field)) is not int or metadata[field] < 0:
                raise ValueError("invalid brief counter")
        if not 0 < metadata["content_bytes"] <= self.max_content_bytes:
            raise ValueError("invalid brief content size")
        for field in ("provider", "model"):
            value = metadata.get(field)
            if not isinstance(value, str) or len(value) > 500 or any(ord(char) < 32 for char in value):
                raise ValueError("invalid brief provider/model")
        expected = {"schema_version", "id", "title", "created_at", "timezone", "provider", "model",
                    "turns", "tool_calls", "tool_attempts", "content_bytes"}
        if set(metadata) != expected:
            raise ValueError("unexpected brief metadata fields")

    def get(self, brief_id: str) -> dict:
        path = self._path(brief_id)
        with path.open("rb") as handle:
            metadata = self._read_metadata(handle, brief_id)
            body = handle.read(self.max_content_bytes + 1)
        if len(body) != metadata["content_bytes"] or len(body) > self.max_content_bytes:
            raise ValueError("brief body is incomplete or exceeds its size limit")
        content = body.decode("utf-8")
        if not content.strip() or "\x00" in content:
            raise ValueError("invalid brief body")
        return {**self._public_metadata(metadata, path), "content": content}

    def list(self, *, limit: int = 20, before: str | None = None) -> list[dict]:
        """Newest ids first; use the last returned id as the next page's cursor."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("history limit must be an integer between 1 and 100")
        if before is not None and (not isinstance(before, str) or not ID_PATTERN.fullmatch(before)):
            raise ValueError("invalid history cursor")
        directory = self._directory()
        if not directory.exists():
            return []
        # Select metadata only, retaining at most limit entries while scanning.
        def records():
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not entry.name.endswith(".md"):
                        continue
                    brief_id = entry.name[:-3]
                    if not ID_PATTERN.fullmatch(brief_id) or (before is not None and brief_id >= before):
                        continue
                    try:
                        path = self._path(brief_id)
                        with path.open("rb") as handle:
                            metadata = self._read_metadata(handle, brief_id)
                        yield self._public_metadata(metadata, path)
                    except (OSError, ValueError, RuntimeError):
                        # An unrelated, damaged or linked file is not an archive record.
                        continue
        return heapq.nlargest(limit, records(), key=lambda record: record["id"])
