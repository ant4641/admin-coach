"""組 messages、解析模型回覆。

規則檔原文 = 唯一的 system message。輸出格式（clarify / vfp 兩種 JSON）由規則檔第 5 章規定，
這裡不附加任何格式說明，只負責組 messages 與解析。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from coach.inputs import Q_INDUSTRY, Q_NAME, Q_PRODUCT, Q_SALES, UNFILLED
from coach.rules import Rules


@dataclass
class CoachReply:
    status: str  # clarify | vfp | unknown（解析失敗）
    question: str = ""
    vfp: str = ""
    byproducts: list[dict] = field(default_factory=list)
    parse_error: str | None = None

    def as_history(self) -> dict:
        """存進對話紀錄、之後原樣回放給模型的樣子（只有 clarify 會用到）。"""
        return {"status": self.status, "question": self.question}


def inputs_message(inputs: dict) -> str:
    """送給模型的輸入：稱呼、產業、產品、賣得如何。自評分數與原因不送。"""

    def val(v: str) -> str:
        return v.strip() or UNFILLED

    return (
        f"## {Q_NAME}\n{inputs['name']}\n\n"
        f"## {Q_INDUSTRY}\n{inputs['industry']}\n\n"
        f"## {Q_PRODUCT}\n{val(inputs['product'])}\n\n"
        f"## {Q_SALES}\n{val(inputs['sales'])}"
    )


def build_messages(rules: Rules, inputs: dict, exchanges: list[dict]) -> list[dict[str, str]]:
    """規則 → 輸入 → 之前一問一答的回放。exchanges 每筆 {"coach": dict, "manager": str | None}；還沒回答的那筆不放。"""
    msgs = [{"role": "system", "content": rules.text}, {"role": "user", "content": inputs_message(inputs)}]
    for ex in exchanges:
        if ex.get("manager") is None:
            continue
        msgs.append({"role": "assistant", "content": json.dumps(ex["coach"], ensure_ascii=False)})
        msgs.append({"role": "user", "content": ex["manager"]})
    return msgs


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def validate_byproducts(items) -> list[dict]:
    """每個副產品都要有非空的 name 和 description，且至少一個。不合格丟 ValueError。"""
    if not isinstance(items, list) or not items:
        raise ValueError("byproducts 必須是至少一項的陣列")
    out = []
    for i, it in enumerate(items, start=1):
        name = _text(it.get("name")) if isinstance(it, dict) else ""
        desc = _text(it.get("description")) if isinstance(it, dict) else ""
        if not name or not desc:
            raise ValueError(f"第 {i} 個副產品缺少 name 或 description")
        out.append({"name": name, "description": desc})
    return out


def parse_reply(raw: str) -> CoachReply:
    text = _FENCE.sub("", raw.strip())
    try:
        data = json.loads(text)
        status = data.get("status")
        if status == "clarify":
            question = _text(data.get("question"))
            if not question:
                raise ValueError("clarify 缺少 question")
            return CoachReply(status="clarify", question=question)
        if status == "vfp":
            vfp = _text(data.get("vfp"))
            if not vfp:
                raise ValueError("vfp 缺少 vfp 內容")
            return CoachReply(status="vfp", vfp=vfp, byproducts=validate_byproducts(data.get("byproducts")))
        raise ValueError(f"status 不合法：{status!r}")
    except (json.JSONDecodeError, ValueError, AttributeError, TypeError) as e:
        return CoachReply(status="unknown", parse_error=str(e))
