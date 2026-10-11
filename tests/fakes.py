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


def reply(status: str) -> dict:
    return {"status": status, "missing": [] if status == "plan" else ["x"], "message": f"scripted {status}"}
