"""測試替身。只給 pytest 用，不屬於正式程式。

刻意不看輸入內容：回覆完全由測試指定，所以不會「配合」任何案例。
"""

from __future__ import annotations

import json

from coach.llm import LLMResult


class ScriptedLLM:
    """每次呼叫都回測試指定的固定回覆，並記下收到的 messages。"""

    provider = "scripted"
    model = "scripted"
    temperature = None

    def __init__(self, reply: dict | str) -> None:
        self.text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> LLMResult:
        self.calls.append(messages)
        return LLMResult(text=self.text, model=self.model, latency_ms=0)


class SequenceLLM:
    """依序回覆；回覆用完還被呼叫就丟錯（代表流程多打了一次模型，或少寫了測試劇本）。"""

    provider = "scripted"
    model = "scripted"
    temperature = None

    def __init__(self, *replies: dict | str) -> None:
        self.queue = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in replies]
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> LLMResult:
        self.calls.append(messages)
        if not self.queue:
            raise AssertionError("劇本用完了，但流程又呼叫了一次模型")
        return LLMResult(text=self.queue.pop(0), model=self.model, latency_ms=0)


def clarify(question: str = "一個問題") -> dict:
    return {"status": "clarify", "question": question}


def vfp(text: str = "得到好結果的客人", byproducts: list[dict] | None = None) -> dict:
    items = byproducts if byproducts is not None else [{"name": "備料", "description": "先備好"}, {"name": "交付", "description": "交給客人"}]
    return {"status": "vfp", "vfp": text, "byproducts": items}
