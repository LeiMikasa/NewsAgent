"""配置集中在这里；优先级：显式参数 > 环境变量 > .env > 默认值。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


@dataclass
class Settings:
    provider: str = "mock"
    model: str = "mock-1"
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    workdir: Path = field(default_factory=Path.cwd)
    max_turns: int = 30
    max_tokens: int = 4096
    model_timeout: float = 120
    bash_timeout: float = 30
    max_tool_output: int = 20000
    max_file_bytes: int = 2_000_000
    shell: str = "auto"
    allow_bash: bool = True
    memory_enabled: bool = True
    memory_auto_extract: bool = True
    memory_dir: Path | None = None
    tavily_api_key: str | None = field(default=None, repr=False)
    tavily_timeout: float = 30
    max_tool_retries: int = 2
    max_tool_repairs: int = 2
    briefs_dir: Path | None = None
    email_enabled: bool = False
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_username: str | None = None
    smtp_password: str | None = field(default=None, repr=False)
    email_to: str | None = None
    smtp_timeout: float = 30

    def __post_init__(self) -> None:
        self.workdir = Path(self.workdir).expanduser().resolve()
        self.provider = self.provider.strip().lower()
        if self.provider not in {"mock", "anthropic", "deepseek"}:
            raise ValueError("provider must be mock, anthropic or deepseek")
        if self.provider == "deepseek" and not self.base_url:
            self.base_url = "https://api.deepseek.com/anthropic"
        if not self.workdir.is_dir():
            raise ValueError(f"workdir is not a directory: {self.workdir}")
        memory_path = Path(self.memory_dir).expanduser() if self.memory_dir is not None else Path(".agent/memory")
        self.memory_dir = (memory_path if memory_path.is_absolute() else self.workdir / memory_path).resolve()
        if not self.memory_dir.is_relative_to(self.workdir) or self.memory_dir == self.workdir:
            raise ValueError("memory_dir must be a subdirectory of workdir")
        briefs_path = Path(self.briefs_dir).expanduser() if self.briefs_dir is not None else Path(".agent/briefs")
        self.briefs_dir = (briefs_path if briefs_path.is_absolute() else self.workdir / briefs_path).resolve()
        if not self.briefs_dir.is_relative_to(self.workdir) or self.briefs_dir == self.workdir:
            raise ValueError("briefs_dir must be a subdirectory of workdir")
        if self.briefs_dir.is_relative_to(self.memory_dir) or self.memory_dir.is_relative_to(self.briefs_dir):
            raise ValueError("briefs_dir must not overlap memory_dir")
        for name in ("memory_enabled", "memory_auto_extract", "email_enabled"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        for name, maximum in (("max_tool_retries", 5), ("max_tool_repairs", 10)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 0 and {maximum}")
        for name in ("max_turns", "max_tokens", "max_tool_output", "max_file_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.tavily_api_key is not None:
            if not isinstance(self.tavily_api_key, str) or any(c in self.tavily_api_key for c in "\r\n"):
                raise ValueError("tavily_api_key must be a single-line string")
            self.tavily_api_key = self.tavily_api_key.strip()
        if (isinstance(self.tavily_timeout, bool) or not isinstance(self.tavily_timeout, (int, float))
                or not 0 < self.tavily_timeout <= 120):
            raise ValueError("tavily_timeout must be between 0 and 120 seconds")
        for name in ("model_timeout", "bash_timeout"):
            value = getattr(self, name)
            if not 0 < value <= 3600:
                raise ValueError(f"{name} must be between 0 and 3600 seconds")
        if self.provider != "mock" and (not self.model or self.model == "mock-1"):
            raise ValueError("set MODEL_ID to the model supported by your provider")
        if not isinstance(self.smtp_host, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", self.smtp_host):
            raise ValueError("smtp_host must be a hostname")
        if isinstance(self.smtp_port, bool) or not isinstance(self.smtp_port, int) or not 1 <= self.smtp_port <= 65535:
            raise ValueError("smtp_port must be between 1 and 65535")
        if (isinstance(self.smtp_timeout, bool) or not isinstance(self.smtp_timeout, (int, float))
                or not 0 < self.smtp_timeout <= 120):
            raise ValueError("smtp_timeout must be between 0 and 120 seconds")
        for name in ("smtp_username", "email_to"):
            value = getattr(self, name)
            if value is not None:
                if (not isinstance(value, str) or len(value) > 320
                        or not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", value.strip())):
                    raise ValueError(f"{name} must be a single email address")
                setattr(self, name, value.strip())
        if self.smtp_password is not None:
            if (not isinstance(self.smtp_password, str) or len(self.smtp_password) > 4096
                    or any(ord(char) < 32 for char in self.smtp_password)):
                raise ValueError("smtp_password must be a single-line authorization code")
            self.smtp_password = self.smtp_password.strip()


def load_settings(env_file: str | Path | None = None, **overrides) -> Settings:
    path = Path(env_file) if env_file is not None else Path.cwd() / ".env" # 确定读配置文件
    if env_file is not None and not path.is_file():
        raise ValueError(f"env file not found: {path}")
    values = dict(dotenv_values(path)) if path.is_file() else {} #
    values.update(os.environ) # 让进程环境变量覆盖文件里的同名值
    mapping = {
        "provider": ("AGENT_PROVIDER", str),
        "model": ("MODEL_ID", str),
        "api_key": ("ANTHROPIC_API_KEY", str),
        "base_url": ("ANTHROPIC_BASE_URL", str),
        "workdir": ("AGENT_WORKDIR", Path),
        "max_turns": ("AGENT_MAX_TURNS", int),
        "max_tokens": ("AGENT_MAX_TOKENS", int),
        "model_timeout": ("AGENT_MODEL_TIMEOUT", float),
        "bash_timeout": ("AGENT_BASH_TIMEOUT", float),
        "max_tool_output": ("AGENT_MAX_TOOL_OUTPUT", int),
        "max_file_bytes": ("AGENT_MAX_FILE_BYTES", int),
        "shell": ("AGENT_SHELL", str),
        "memory_dir": ("AGENT_MEMORY_DIR", Path),
        "briefs_dir": ("AGENT_BRIEFS_DIR", Path),
        "tavily_api_key": ("TAVILY_API_KEY", str),
        "tavily_timeout": ("TAVILY_TIMEOUT", float),
        "max_tool_retries": ("AGENT_MAX_TOOL_RETRIES", int),
        "max_tool_repairs": ("AGENT_MAX_TOOL_REPAIRS", int),
        "smtp_host": ("SMTP_HOST", str),
        "smtp_port": ("SMTP_PORT", int),
        "smtp_username": ("SMTP_USERNAME", str),
        "smtp_password": ("SMTP_PASSWORD", str),
        "email_to": ("EMAIL_TO", str),
        "smtp_timeout": ("SMTP_TIMEOUT", float),
    }
    resolved = {}
    for field_name, (env_name, converter) in mapping.items():
        value = values.get(env_name)
        if value:
            resolved[field_name] = converter(value)
    for field_name, env_name in (("allow_bash", "AGENT_ALLOW_BASH"),
                                 ("memory_enabled", "AGENT_MEMORY_ENABLED"),
                                 ("memory_auto_extract", "AGENT_MEMORY_AUTO_EXTRACT"),
                                 ("email_enabled", "EMAIL_ENABLED")):
        raw_bool = values.get(env_name)
        if raw_bool is not None:
            if raw_bool.lower() not in {"true", "false", "1", "0"}:
                raise ValueError(f"{env_name} must be true, false, 1 or 0")
            resolved[field_name] = raw_bool.lower() in {"true", "1"}
    resolved.update({key: value for key, value in overrides.items() if value is not None})
    return Settings(**resolved)
