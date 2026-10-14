"""讀取測試案例。

格式：
---
title: 麵店
expected_status: plan      # plan | clarify
---
（以下整段當作使用者訊息送給模型）

一般案例（kind 省略）：內文有「## 稱呼 / 理想情境 / 現在情境」。

週回報案例（kind: weekly）：測「已進入執行 → 週回報 → 新等級表」。
front matter 多：
  confidence: 85         # 進入執行用的信心分數（>= 80），由案例提供，不預設
  base_case: 01_design_company   # 沒有「## 上週等級表」時，用這個案例（呼叫模型）產出第一版
  expected_status: plan  # 週回報這一步的結果：plan（產出新表）或 clarify（資訊不足、追問）
內文多這幾段（「## 上週等級表」可省略，省略時 稱呼/理想/現況 也取自 base_case）：
  ## 稱呼 / ## 理想情境 / ## 現在情境 / ## 上週等級表
  ## 實際狀況 / ## 本週成果分數 / ## 哪些達成 / ## 哪些沒達成 / ## 原因
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

VALID_STATUS = {"plan", "clarify"}
VALID_KIND = {"intake", "weekly"}


@dataclass(frozen=True)
class Case:
    path: Path
    name: str
    meta: dict[str, str] = field(default_factory=dict)
    body: str = ""
    sha256: str = ""

    @property
    def expected_status(self) -> str | None:
        return self.meta.get("expected_status")

    @property
    def kind(self) -> str:
        return self.meta.get("kind", "intake")

    @property
    def sections(self) -> dict[str, str]:
        return parse_sections(self.body)

    def intake_fields(self) -> tuple[str, str, str]:
        """(稱呼, 理想情境, 現在情境)"""
        s = self.sections
        return s["稱呼"], s["理想情境"], s["現在情境"]

    def weekly_fields(self) -> dict:
        """weekly_report() 的參數"""
        s = self.sections
        return {
            "actual": s["實際狀況"],
            "score": int(s["本週成果分數"]),
            "achieved": s.get("哪些達成", ""),
            "not_achieved": s.get("哪些沒達成", ""),
            "reason": s.get("原因", ""),
        }


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1 :]).strip()
        key, sep, value = line.partition(":")
        if sep:
            # 去掉行尾註解
            meta[key.strip()] = value.split("#", 1)[0].strip()
    raise ValueError("front matter 沒有結尾的 ---")


def parse_sections(body: str) -> dict[str, str]:
    """依「## 標題」把內文切段；第一個標題之前的文字忽略。"""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in body.splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
        elif current is not None:
            current.append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _validate_weekly(path: Path, meta: dict[str, str], body: str) -> None:
    sections = parse_sections(body)
    need = ["實際狀況", "本週成果分數"]
    if "上週等級表" in sections:
        need += ["稱呼", "理想情境", "現在情境"]
    elif not meta.get("base_case"):
        raise ValueError(f"{path.name}: 週回報案例要有「## 上週等級表」，或在 front matter 指定 base_case")
    missing = [k for k in need if not sections.get(k)]
    if missing:
        raise ValueError(f"{path.name}: 週回報案例缺少段落：{'、'.join(missing)}")
    for label, value, lo in (("confidence", meta.get("confidence", ""), 80), ("本週成果分數", sections["本週成果分數"], 0)):
        if not value.isdigit() or not lo <= int(value) <= 100:
            raise ValueError(f"{path.name}: {label} 必須是 {lo}～100 的整數，收到 {value!r}")


def load_case(path: Path) -> Case:
    raw = path.read_bytes()
    meta, body = _parse_front_matter(raw.decode("utf-8"))
    status = meta.get("expected_status")
    if status and status not in VALID_STATUS:
        raise ValueError(f"{path.name}: expected_status 必須是 {VALID_STATUS}，收到 {status!r}")
    if not body:
        raise ValueError(f"{path.name}: 內容是空的")
    kind = meta.get("kind", "intake")
    if kind not in VALID_KIND:
        raise ValueError(f"{path.name}: kind 必須是 {VALID_KIND}，收到 {kind!r}")
    if kind == "weekly":
        _validate_weekly(path, meta, body)
    return Case(path=path, name=path.stem, meta=meta, body=body, sha256=hashlib.sha256(raw).hexdigest())


def load_cases(directory: Path) -> list[Case]:
    return [load_case(p) for p in sorted(directory.glob("*.md"))]
