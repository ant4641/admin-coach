"""讀取測試案例。

格式：
---
title: 案例名稱
expected_status: clarify      # clarify | vfp
todo: 待補                    # 選用；有這行的案例會被跳過（內容還沒提供）
---
## 稱呼
## 產業                       # 規則檔 2.1 的編號 1～20（編號後面可附說明文字，只取編號）
## 其他產業說明               # 選 20 才需要
## 產品                       # 可空白
## 賣得如何                   # 可空白
## 自評分數                   # 可空白，1～10
## 原因                       # 可空白
## answers                    # 可省略；依序回答追問，一行 "- " 一個回答
- 第一個回答
- 第二個回答

沒有 answers 的案例只跑第一輪（不替主管編回答）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

VALID_STATUS = {"clarify", "vfp"}


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
    def todo(self) -> str | None:
        return self.meta.get("todo") or None

    @property
    def sections(self) -> dict[str, str]:
        return parse_sections(self.body)

    def start_fields(self) -> dict:
        """CoachService.start() 的參數"""
        s = self.sections
        score = s.get("自評分數", "")
        return {
            "name": s.get("稱呼", ""),
            "industry_no": industry_number(s["產業"]),
            "industry_other": s.get("其他產業說明", ""),
            "product": s.get("產品", ""),
            "sales": s.get("賣得如何", ""),
            "score": int(score) if score else None,
            "reason": s.get("原因", ""),
        }

    def answers(self) -> list[str]:
        return parse_answers(self.sections.get("answers", ""))


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


def parse_answers(text: str) -> list[str]:
    """「- 」開頭的行是一個回答；接在後面沒有「- 」的行算同一個回答的下一行。"""
    answers: list[str] = []
    for line in text.splitlines():
        if line.startswith("- "):
            answers.append(line[2:].strip())
        elif answers and line.strip():
            answers[-1] += "\n" + line.strip()
    return answers


def industry_number(text: str) -> int:
    """「8」或「8 資訊、軟體與網路服務」→ 8；開頭不是編號丟 ValueError。"""
    head = text.split(maxsplit=1)[0] if text.strip() else ""
    if not head.isdigit():
        raise ValueError(f"「## 產業」必須以編號開頭，收到 {text!r}")
    return int(head)


def load_case(path: Path) -> Case:
    raw = path.read_bytes()
    meta, body = _parse_front_matter(raw.decode("utf-8"))
    status = meta.get("expected_status")
    if status and status not in VALID_STATUS:
        raise ValueError(f"{path.name}: expected_status 必須是 {VALID_STATUS}，收到 {status!r}")
    if not body:
        raise ValueError(f"{path.name}: 內容是空的")
    if not meta.get("todo"):
        sections = parse_sections(body)
        if not sections.get("稱呼"):
            raise ValueError(f"{path.name}: 缺少「## 稱呼」")
        try:
            industry_number(sections.get("產業", ""))
        except ValueError as e:
            raise ValueError(f"{path.name}: {e}") from e
        if sections.get("自評分數") and not sections["自評分數"].isdigit():
            raise ValueError(f"{path.name}: 「## 自評分數」必須是整數")
    return Case(path=path, name=path.stem, meta=meta, body=body, sha256=hashlib.sha256(raw).hexdigest())


def load_cases(directory: Path) -> list[Case]:
    return [load_case(p) for p in sorted(directory.glob("*.md"))]
