"""組 messages、解析模型回覆。

規則檔原文 = 第一則 system message（不改動）。
程式另外附加一則「輸出約定」，讓回覆可被程式判讀（plan / clarify）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from coach.rules import Rules

OUTPUT_CONTRACT = """\
【系統輸出約定（由程式附加，不屬於規則檔；與規則檔衝突時以規則檔的內容規則為準，本約定只規範回覆的格式）】
你的回覆必須是單一 JSON 物件，不要有 JSON 以外的任何文字：
{
  "status": "clarify" 或 "plan",
  "missing": ["尚缺的資訊項目，status=plan 時為空陣列"],
  "message": "要顯示給主管的內容（Markdown）"
}
- 依規則 2.3 判斷：理想畫面尚未具體，或現況資訊不足 → status = "clarify"，message 為要問主管的話（依規則一次只問一個問題）。
- 理想畫面已具體且現況資訊已足 → status = "plan"，message 為依規則第 5 節格式產出的完整行政管理等級表。
"""


@dataclass
class CoachReply:
    status: str  # plan | clarify | unknown（解析失敗）
    message: str
    missing: list[str] = field(default_factory=list)
    parse_error: str | None = None


def build_messages(rules: Rules, user_input: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": rules.text},
        {"role": "system", "content": OUTPUT_CONTRACT},
        {"role": "user", "content": user_input},
    ]


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_reply(raw: str) -> CoachReply:
    text = _FENCE.sub("", raw.strip())
    try:
        data = json.loads(text)
        status = data.get("status")
        if status not in ("plan", "clarify"):
            raise ValueError(f"status 不合法：{status!r}")
        missing = data.get("missing") or []
        return CoachReply(status=status, message=str(data.get("message", "")), missing=[str(m) for m in missing])
    except (json.JSONDecodeError, ValueError, AttributeError) as e:
        return CoachReply(status="unknown", message=raw, parse_error=str(e))
