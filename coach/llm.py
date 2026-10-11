"""LLM 抽象層。換供應商只需在這裡加一個 class 並註冊到 get_llm()。

環境變數見 .env.example。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class LLMResult:
    text: str
    model: str
    latency_ms: int
    usage: dict[str, int] = field(default_factory=dict)


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, messages: list[dict[str, str]]) -> LLMResult: ...


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少環境變數 {name}（請參考 .env.example 建立 .env）")
    return value


def _temperature() -> float | None:
    value = os.getenv("LLM_TEMPERATURE", "").strip()
    return float(value) if value else None


class _OpenAICompatible:
    """Azure OpenAI 與 OpenAI 共用的 chat.completions 呼叫。"""

    provider = ""
    model = ""

    def __init__(self, client) -> None:
        self._client = client
        self.temperature = _temperature()

    def complete(self, messages: list[dict[str, str]]) -> LLMResult:
        kwargs: dict = {"model": self.model, "messages": messages, "response_format": {"type": "json_object"}}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        start = time.perf_counter()
        resp = self._client.chat.completions.create(**kwargs)
        latency_ms = int((time.perf_counter() - start) * 1000)
        usage = {}
        if resp.usage:
            usage = {"prompt_tokens": resp.usage.prompt_tokens, "completion_tokens": resp.usage.completion_tokens}
        return LLMResult(
            text=resp.choices[0].message.content or "",
            model=resp.model or self.model,
            latency_ms=latency_ms,
            usage=usage,
        )


class AzureOpenAIClient(_OpenAICompatible):
    provider = "azure"

    def __init__(self) -> None:
        from openai import AzureOpenAI

        self.model = _require("AZURE_OPENAI_DEPLOYMENT")
        super().__init__(
            AzureOpenAI(
                azure_endpoint=_require("AZURE_OPENAI_ENDPOINT"),
                api_key=_require("AZURE_OPENAI_API_KEY"),
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            )
        )


class OpenAIClient(_OpenAICompatible):
    provider = "openai"

    def __init__(self) -> None:
        from openai import OpenAI

        self.model = os.getenv("OPENAI_MODEL", "gpt-4o")
        super().__init__(OpenAI(api_key=_require("OPENAI_API_KEY")))


_PROVIDERS: dict[str, type] = {
    "azure": AzureOpenAIClient,
    "openai": OpenAIClient,
}


def get_llm(provider: str | None = None) -> LLMClient:
    name = (provider or os.getenv("LLM_PROVIDER", "azure")).strip().lower()
    if name not in _PROVIDERS:
        raise RuntimeError(f"未知的 LLM_PROVIDER：{name}（可用：{', '.join(_PROVIDERS)}）")
    return _PROVIDERS[name]()
