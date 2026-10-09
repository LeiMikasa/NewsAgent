"""与 HarnessAgent 相同的窄模型接口；loop 不依赖 SDK 对象。"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .config import Settings


@dataclass
class LLMResponse:
    content: list[dict] = field(default_factory=list)
    stop_reason: str | None = "end_turn"
    usage: dict | None = None

    def text(self) -> str:
        return "\n".join(block["text"] for block in self.content
                         if block.get("type") == "text" and block.get("text"))


class LLMClient(Protocol):
    def create(self, *, system: str, messages: list[dict], tools: list[dict],
               max_tokens: int) -> LLMResponse: ...


def normalize_response(raw: Any) -> LLMResponse:
    if isinstance(raw, LLMResponse):
        return copy.deepcopy(raw)
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(exclude_none=True)
    if not isinstance(raw, dict) or not isinstance(raw.get("content"), list):
        raise ValueError("model response requires a content block list")
    content = copy.deepcopy(raw["content"])
    if not all(isinstance(block, dict) for block in content):
        raise ValueError("model response blocks must be objects")
    return LLMResponse(content, raw.get("stop_reason"), copy.deepcopy(raw.get("usage")))


class AnthropicLLM:
    def __init__(self, settings: Settings):
        if not settings.api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for a live provider")
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise RuntimeError("install dependencies: python -m pip install -r requirements.txt") from exc
        kwargs = {"api_key": settings.api_key, "timeout": settings.model_timeout, "max_retries": 2}
        if settings.base_url:
            kwargs["base_url"] = settings.base_url
        self.client = Anthropic(**kwargs)
        self.model = settings.model

    def create(self, *, system: str, messages: list[dict], tools: list[dict],
               max_tokens: int) -> LLMResponse:
        payload = dict(model=self.model, system=system, messages=messages, max_tokens=max_tokens)
        if tools:
            payload["tools"] = tools
        return normalize_response(self.client.messages.create(**payload))

    def close(self) -> None:
        self.client.close()


class MockLLM:
    """脚本每项是文本、{tool,input}、此类项的列表或完整 LLMResponse。

    responder(call_index, messages, tools) 可实现按工具结果决定下一步的离线模型。
    无脚本时只返回带 mock 标记的文本，不假装完成任务。
    """

    def __init__(self, script: list | None = None, responder: Callable | None = None):
        self.script = copy.deepcopy(script) if script is not None else None
        self.responder = responder
        self.calls: list[dict] = []
        self._tool_counter = 0

    def create(self, *, system: str, messages: list[dict], tools: list[dict],
               max_tokens: int) -> LLMResponse:
        index = len(self.calls)
        self.calls.append(copy.deepcopy(dict(system=system, messages=messages,
                                             tools=tools, max_tokens=max_tokens)))
        if self.responder is not None:
            step = self.responder(index, copy.deepcopy(messages), copy.deepcopy(tools))
        elif self.script is not None:
            if index >= len(self.script):
                raise RuntimeError("mock script exhausted before a final response")
            step = self.script[index]
        else:
            step = "[mock] 运行框架已就绪。使用 --demo 验证工具闭环，配置 .env 后接入真实模型。"
        if isinstance(step, LLMResponse) or isinstance(step, dict) and "content" in step:
            return normalize_response(step)
        blocks = []
        for item in step if isinstance(step, list) else [step]:
            if isinstance(item, str):
                blocks.append({"type": "text", "text": item})
            elif isinstance(item, dict) and "tool" in item:
                self._tool_counter += 1
                blocks.append({"type": "tool_use", "id": f"mock_{self._tool_counter}",
                               "name": item["tool"], "input": copy.deepcopy(item.get("input", {}))})
            else:
                raise ValueError(f"invalid mock step: {item!r}")
        reason = "tool_use" if any(block["type"] == "tool_use" for block in blocks) else "end_turn"
        return LLMResponse(blocks, reason)


def build_client(settings: Settings, *, script: list | None = None,
                 responder: Callable | None = None) -> LLMClient:
    if settings.provider == "mock":
        return MockLLM(script, responder)
    if script is not None or responder is not None:
        raise ValueError("script/responder require the mock provider")
    return AnthropicLLM(settings)
