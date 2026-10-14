"""Session 存取層。每個對話一個 JSON 檔：outputs/sessions/<session_id>.json。

流程程式（flow.py）只透過 SessionStore 介面存取。之後換資料庫時，
實作同樣的 new_id / load / save 並換掉 create_app 的預設 store 即可，其他不用動。
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Protocol

_VALID_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SessionNotFound(LookupError):
    pass


class SessionStore(Protocol):
    def new_id(self) -> str: ...

    def load(self, session_id: str) -> dict: ...

    def save(self, session: dict) -> None: ...


class JsonFileStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def new_id(self) -> str:
        return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"

    def _path(self, session_id: str) -> Path:
        # id 會變成檔名，只收安全字元，擋掉 ../ 之類
        if not _VALID_ID.match(session_id):
            raise SessionNotFound(session_id)
        return self.directory / f"{session_id}.json"

    def load(self, session_id: str) -> dict:
        path = self._path(session_id)
        if not path.exists():
            raise SessionNotFound(session_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, session: dict) -> None:
        path = self._path(session["id"])
        self.directory.mkdir(parents=True, exist_ok=True)
        # 先寫暫存檔再取代，寫到一半當掉也不會留下壞掉的 JSON
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
