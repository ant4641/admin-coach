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


BYPRODUCT_KEYS = ("task", "detail", "product")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def validate_byproducts(items) -> list[dict]:
    """模型回覆：每一項的 task、detail、product 都要是非空字串，且至少一項。不合格丟 ValueError。"""
    if not isinstance(items, list) or not items:
        raise ValueError("byproducts 必須是至少一項的陣列")
    out = []
    for i, it in enumerate(items, start=1):
        if not isinstance(it, dict) or not all(isinstance(it.get(k), str) and it[k].strip() for k in BYPRODUCT_KEYS):
            raise ValueError(f"第 {i} 項缺少 task、detail 或 product（或不是非空字串）")
        out.append({k: it[k].strip() for k in BYPRODUCT_KEYS})
    return out


class ValidationErrors(ValueError):
    """驗證失敗：str(e) 是簡短中文說明（第一個問題），errors 是 [{row, field, message}]（row 從 1 開始）。"""

    def __init__(self, detail: str, errors: list[dict] | None = None) -> None:
        super().__init__(detail)
        self.errors = errors or []


def validate_edited_byproducts(items, staff_ids: set[str] | None = None) -> list[dict]:
    """主管修改版：task 與 product 不可為空，detail 可以空；assignee 為 None 或存在於 staff_ids（staff_ids 為 None 時不檢查）。"""
    if not isinstance(items, list) or not items:
        raise ValidationErrors("副產品清單至少要有一列")
    out, errors, details = [], [], []

    def bad(row: int, field: str, short: str, long: str) -> None:
        errors.append({"row": row, "field": field, "message": short})
        details.append(long)

    for i, it in enumerate(items, start=1):
        it = it if isinstance(it, dict) else {}
        row = {k: _text(it.get(k)) for k in BYPRODUCT_KEYS}
        assignee = it.get("assignee")
        assignee = assignee.strip() if isinstance(assignee, str) and assignee.strip() else None
        if not row["task"]:
            bad(i, "task", "工作項目不得為空", f"第 {i} 列的工作項目不可為空")
        if not row["product"]:
            bad(i, "product", "副產品不得為空", f"第 {i} 列的副產品不可為空")
        if assignee is not None and staff_ids is not None and assignee not in staff_ids:
            bad(i, "assignee", "負責人不在員工名單中", f"第 {i} 列的負責人不在員工名單中")
        out.append({**row, "assignee": assignee})
    if errors:
        raise ValidationErrors(details[0], errors)
    return out


def upgrade_byproduct(it: dict) -> dict:
    """舊格式 {name, description} 轉成 {task, detail, product, assignee}（product 留空、assignee 為 None）；新格式補齊欄位。只在記憶體轉換。"""
    if "task" in it or "product" in it or "detail" in it:
        row = {k: it.get(k) or "" for k in BYPRODUCT_KEYS}
    else:
        row = {"task": it.get("name") or "", "detail": it.get("description") or "", "product": ""}
    assignee = it.get("assignee")
    row["assignee"] = assignee if isinstance(assignee, str) and assignee else None
    return row


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
