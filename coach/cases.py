"""讀取測試案例。

格式：
---
title: 麵店
expected_status: plan      # plan | clarify
---
（以下整段當作使用者訊息送給模型）
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

VALID_STATUS = {"plan", "clarify"}


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


def load_case(path: Path) -> Case:
    raw = path.read_bytes()
    meta, body = _parse_front_matter(raw.decode("utf-8"))
    status = meta.get("expected_status")
    if status and status not in VALID_STATUS:
        raise ValueError(f"{path.name}: expected_status 必須是 {VALID_STATUS}，收到 {status!r}")
    if not body:
        raise ValueError(f"{path.name}: 內容是空的")
    return Case(path=path, name=path.stem, meta=meta, body=body, sha256=hashlib.sha256(raw).hexdigest())


def load_cases(directory: Path) -> list[Case]:
    return [load_case(p) for p in sorted(directory.glob("*.md"))]
