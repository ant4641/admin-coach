"""六項輸入：題目文字、產業清單（從規則檔 2.1 讀取）、驗證。"""

from __future__ import annotations

import re

# 題目文字照規則檔（第 2 章標題、第 8 章範例小標）；tests 會檢查它們都出現在規則檔裡
Q_NAME = "該如何稱呼您?"
Q_INDUSTRY = "你在哪個產業?"
Q_PRODUCT = "你覺得自己/團隊/公司在賣什麼產品?"
Q_SALES = "賣的如何?"
Q_SCORE = "為自己的營運打個分數 (1~10分)?"
Q_REASON = "為何給這個分數?"

UNFILLED = "（未填）"
_INDUSTRY_ITEM = re.compile(r"^\s*(\d+)\.\s*(.+?)\s*$")


def parse_industries(rules_text: str) -> list[str]:
    """規則檔 2.1 的編號清單，每項為編號後面的完整原文。編號必須從 1 連續。"""
    items: list[str] = []
    in_section = False
    for line in rules_text.splitlines():
        if line.startswith("### 2.1"):
            in_section = True
        elif in_section and line.startswith("#"):
            break
        elif in_section:
            m = _INDUSTRY_ITEM.match(line)
            if m:
                if int(m.group(1)) != len(items) + 1:
                    raise ValueError(f"規則檔 2.1 的編號不連續：{line.strip()!r}")
                items.append(m.group(2))
    if not items:
        raise ValueError("規則檔找不到 2.1 的產業清單")
    return items


def is_other(industry: str) -> bool:
    return industry.startswith("其他")


def build_inputs(
    industries: list[str],
    name: str,
    industry_no: int,
    industry_other: str = "",
    product: str = "",
    sales: str = "",
    score: int | None = None,
    reason: str = "",
) -> dict:
    """驗證並整理成存進 session 的 inputs。只有稱呼、產業必填。錯誤丟 ValueError。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("稱呼不可空白")
    if isinstance(industry_no, bool) or not isinstance(industry_no, int) or not 1 <= industry_no <= len(industries):
        raise ValueError(f"產業必須是 1～{len(industries)} 的編號")
    industry = industries[industry_no - 1]
    if is_other(industry):
        other = (industry_other or "").strip()
        if not other:
            raise ValueError("選「其他」時必須填寫產業")
        industry = f"其他：{other}"
    if score is not None and (isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 10):
        raise ValueError("營運自評分數必須是 1～10 的整數")
    return {
        "name": name,
        "industry_no": industry_no,
        "industry": industry,
        "product": (product or "").strip(),
        "sales": (sales or "").strip(),
        "score": score,  # 只存檔，不送模型
        "reason": (reason or "").strip(),  # 只存檔，不送模型
    }
