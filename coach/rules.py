"""讀取規則檔並計算版本資訊。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "coach_rules.md"

# 版本紀錄表的資料列：| v0.1 | 2026-09-28 | ... |
_VERSION_ROW = re.compile(r"^\|\s*(v[\w.\-]+)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|", re.MULTILINE)


@dataclass(frozen=True)
class Rules:
    path: Path
    text: str
    version: str  # 版本紀錄表最後一列，找不到則 "unversioned"
    version_date: str
    sha256: str
    mtime: str

    @property
    def tag(self) -> str:
        """檔名用：v0.1-1a2b3c4d"""
        return f"{self.version}-{self.sha256[:8]}"


def load_rules(path: Path = DEFAULT_RULES_PATH) -> Rules:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    rows = _VERSION_ROW.findall(text)
    version, version_date = rows[-1] if rows else ("unversioned", "")
    mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    return Rules(
        path=path,
        text=text,
        version=version,
        version_date=version_date,
        sha256=hashlib.sha256(raw).hexdigest(),
        mtime=mtime,
    )
