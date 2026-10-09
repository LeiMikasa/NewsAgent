"""配置集中在这里；优先级：显式参数 > 环境变量 > .env > 默认值。"""

from __future__ import annotations

import os
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

    def __post_init__(self) -> None:
        self.workdir = Path(self.workdir).expanduser().resolve()
        self.provider = self.provider.strip().lower()
        if self.provider not in {"mock", "anthropic", "deepseek"}:
            raise ValueError("provider must be mock, anthropic or deepseek")
        if self.provider == "deepseek" and not self.base_url:
            self.base_url = "https://api.deepseek.com/anthropic"
        if not self.workdir.is_dir():
            raise ValueError(f"workdir is not a directory: {self.workdir}")
        for name in ("max_turns", "max_tokens", "max_tool_output", "max_file_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("model_timeout", "bash_timeout"):
            value = getattr(self, name)
            if not 0 < value <= 3600:
                raise ValueError(f"{name} must be between 0 and 3600 seconds")
        if self.provider != "mock" and (not self.model or self.model == "mock-1"):
            raise ValueError("set MODEL_ID to the model supported by your provider")


def load_settings(env_file: str | Path | None = None, **overrides) -> Settings:
    path = Path(env_file) if env_file is not None else Path.cwd() / ".env"
    if env_file is not None and not path.is_file():
        raise ValueError(f"env file not found: {path}")
    values = dict(dotenv_values(path)) if path.is_file() else {}
    values.update(os.environ)
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
    }
    resolved = {}
    for field_name, (env_name, converter) in mapping.items():
        value = values.get(env_name)
        if value:
            resolved[field_name] = converter(value)
    raw_bool = values.get("AGENT_ALLOW_BASH")
    if raw_bool is not None:
        if raw_bool.lower() not in {"true", "false", "1", "0"}:
            raise ValueError("AGENT_ALLOW_BASH must be true, false, 1 or 0")
        resolved["allow_bash"] = raw_bool.lower() in {"true", "1"}
    resolved.update({key: value for key, value in overrides.items() if value is not None})
    return Settings(**resolved)
