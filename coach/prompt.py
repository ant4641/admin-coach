"""組 messages、解析模型回覆。

規則檔原文 = 第一則 system message（不改動）。
程式另外附加「輸出約定」，讓回覆可被程式判讀（plan / clarify）：
- OUTPUT_CONTRACT：基本約定，所有階段共用
- ADJUST_CONTRACT：信心分數 < 80 的調整階段（規則第 6 章），在基本約定上多幾個欄位
- WEEKLY_CONTRACT：每週回報階段（規則第 7 章），同上
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
- 凡是主管沒有提供、由你建議的數字（目標值、期限、頻率等），一律在數字後標註「（暫定）」，不可當成已確定的事實。
"""

# 兩個階段共用的 changes 欄位說明
_CHANGES_SPEC = """\
  "changes": [   // status=plan 時列出「與上一版不同」的地方；沒有變動的等級不要列
    {"level": 1 到 10 的整數, "name": "等級名稱", "type": "新增" 或 "修改" 或 "刪除",
     "before": "上一版的寫法（新增則為空字串）", "after": "這一版的寫法（刪除則為空字串）", "reason": "為什麼改"}
  ]"""

ADJUST_CONTRACT = f"""\
【系統輸出約定：信心分數低於 80 的調整階段（由程式附加，不屬於規則檔）】
情境：主管對「目前這一版等級表」給的執行信心分數低於 80。依規則第 6 章，已問過「最讓你沒把握的是哪一項？」，主管的回答在對話中。
除基本欄位外，回覆 JSON 可多這些欄位：
{{
  "kind": "info" 或 "upstream_confirm",   // 只在 status=clarify 時填
  "upstream": {{"level": 1 或 2, "item": "該項目目前的寫法", "concern": "為什麼不切實際", "suggestion": "建議怎麼調整"}},   // 只在 kind=upstream_confirm 時填
  "upstream_decision": "accepted" 或 "declined" 或 "none",   // 只在 status=plan 時填
  "advice": "給主管的建議",   // upstream_decision=declined 時必填，其餘為空字串
{_CHANGES_SPEC}
}}
處理順序：
1. 主管的回答不足以知道沒把握的原因 → status=clarify、kind=info，message 只問一個問題。
2. 依規則 3.3 檢查：沒把握的原因是否來自「上游目標訂得不切實際」。上游目標只指你寫在等級表裡的等級 1（目標）與等級 2（目的，含數字指標）；
   絕不評論、也不要求更動主管提供的理想情境與現況。
   - 是，且對話中你還沒提議過 → status=clarify、kind=upstream_confirm；message 指出是哪一項、為什麼、你建議怎麼調整，並問主管要不要調整。你只能提議，不可自行改動上游。
   - 對話中主管已回覆你的上游提議：
     · 同意 → 調整上游，並讓下游配合，upstream_decision=accepted
     · 不同意 → 上游維持原樣，只調整其餘等級；advice 說明「上下游必須協調一致，否則等級表會受阻」並給具體建議；upstream_decision=declined
     · 看不出同意或不同意 → status=clarify、kind=info，只問「要不要調整」這一件事
3. 與上游無關 → 直接針對主管沒把握的項目調整，upstream_decision=none。
4. 調整完成 → status=plan，message 為完整的新版等級表（規則第 5 節格式）。分數由程式另外請主管重給，你不用問。
"""

WEEKLY_CONTRACT = f"""\
【系統輸出約定：每週回報階段（由程式附加，不屬於規則檔）】
情境：主管回報上一版等級表執行一週後的狀況（規則第 7 章）。
除基本欄位外，回覆 JSON 可多這些欄位（只在 status=plan 時填）：
{{
  "analysis": {{
    "positive": ["主管回報中，表示有達成的句子"],
    "negative": ["表示沒達成或不順的句子"],
    "neutral": ["其餘句子"],
    "items": [   // 依規則 7.1、7.2 逐項判斷
      {{"item": "對應等級表的哪一項／哪個步驟", "type": "沒做到" 或 "做了但現場沒有更接近理想畫面" 或 "有效",
       "evidence": "引自主管回報的原話或原意", "action": "保留" 或 "修正" 或 "刪除"}}
    ]
  }},
{_CHANGES_SPEC}
}}
要求：
- 回報資訊不足（例如只寫「還好」）→ status=clarify，message 只問一個問題，不要硬產出（規則 7.5、2.2）。
- 句子分類只依主管實際寫的內容，不補充主管沒說的事實。
- 保留有效的、修正無效的；不責備（規則第 1 章）；理想情境不會一週達成，每週是持續修正的一步。
- 新等級表仍用規則第 5 節格式，message 放完整新表；與上一版相比改了什麼放在 changes。
- 新表要讓主管重新給信心分數；分數由程式另外請主管給，你不用問。
"""


@dataclass
class CoachReply:
    status: str  # plan | clarify | unknown（解析失敗）
    message: str
    missing: list[str] = field(default_factory=list)
    parse_error: str | None = None
    # 以下是階段 2 擴充的欄位，沒有就是預設值
    kind: str | None = None  # clarify 的種類：info | upstream_confirm
    upstream: dict | None = None
    upstream_decision: str | None = None  # accepted | declined | none
    advice: str = ""
    changes: list[dict] = field(default_factory=list)
    analysis: dict | None = None

    def as_history(self) -> dict:
        """存進對話紀錄、之後原樣回放給模型的樣子（只有 clarify 會用到）。"""
        d: dict = {"status": self.status, "missing": self.missing, "message": self.message}
        if self.kind:
            d["kind"] = self.kind
        if self.upstream:
            d["upstream"] = self.upstream
        return d


def build_messages(rules: Rules, user_input: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": rules.text},
        {"role": "system", "content": OUTPUT_CONTRACT},
        {"role": "user", "content": user_input},
    ]


def build_phase_messages(
    rules: Rules, contract: str | None, context: str, exchanges: list[dict]
) -> list[dict[str, str]]:
    """規則 → 基本約定 →（階段約定）→ 情境 → 之前一問一答的回放。

    exchanges 每筆 {"coach": dict, "manager": str | None}；manager 還沒回答的那筆不放進去。
    """
    msgs = [{"role": "system", "content": rules.text}, {"role": "system", "content": OUTPUT_CONTRACT}]
    if contract:
        msgs.append({"role": "system", "content": contract})
    msgs.append({"role": "user", "content": context})
    for ex in exchanges:
        if ex.get("manager") is None:
            continue
        msgs.append({"role": "assistant", "content": json.dumps(ex["coach"], ensure_ascii=False)})
        msgs.append({"role": "user", "content": ex["manager"]})
    return msgs


def intake_input(name: str, ideal: str, current: str) -> str:
    """與 test_cases/ 案例內文同一種格式。"""
    return f"## 稱呼\n{name}\n\n## 理想情境\n{ideal}\n\n## 現在情境\n{current}"


def adjust_context(name: str, ideal: str, current: str, version_no: int, table: str, score: int, asked_upstream: bool) -> str:
    hint = (
        "你已經提議過上游目標了，這次不可以再提議；請依主管的回覆處理。"
        if asked_upstream
        else "你還沒提議過上游目標。"
    )
    return (
        f"{intake_input(name, ideal, current)}\n\n"
        f"## 目前這一版等級表（第 {version_no} 版）\n{table}\n\n"
        f"## 主管給的執行信心分數\n{score}（低於 80）\n\n"
        f"## 程式提示\n{hint}"
    )


def weekly_context(
    name: str, ideal: str, current: str, version_no: int, table: str, report: dict, prior_reports: list[dict]
) -> str:
    def val(v: str) -> str:
        return v.strip() or "（主管未填）"

    def block(r: dict) -> str:
        return (
            f"- 實際狀況：{val(r['actual'])}\n- 本週成果分數：{r['score']}\n- 哪些達成：{val(r['achieved'])}\n"
            f"- 哪些沒達成：{val(r['not_achieved'])}\n- 原因：{val(r['reason'])}"
        )

    parts = [intake_input(name, ideal, current), f"## 上週的等級表（第 {version_no} 版）\n{table}"]
    if prior_reports:
        history = "\n\n".join(f"第 {i} 次回報\n{block(r)}" for i, r in enumerate(prior_reports, start=1))
        parts.append(f"## 更早之前的週回報\n{history}")
    parts.append(f"## 本週回報\n{block(report)}")
    return "\n\n".join(parts)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _dict_list(value) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def parse_reply(raw: str) -> CoachReply:
    text = _FENCE.sub("", raw.strip())
    try:
        data = json.loads(text)
        status = data.get("status")
        if status not in ("plan", "clarify"):
            raise ValueError(f"status 不合法：{status!r}")
        missing = data.get("missing") or []
        upstream = data.get("upstream")
        analysis = data.get("analysis")
        return CoachReply(
            status=status,
            message=str(data.get("message", "")),
            missing=[str(m) for m in missing],
            kind=data.get("kind") or None,
            upstream=upstream if isinstance(upstream, dict) else None,
            upstream_decision=data.get("upstream_decision") or None,
            advice=str(data.get("advice") or ""),
            changes=_dict_list(data.get("changes")),
            analysis=analysis if isinstance(analysis, dict) else None,
        )
    except (json.JSONDecodeError, ValueError, AttributeError, TypeError) as e:
        return CoachReply(status="unknown", message=raw, parse_error=str(e))
