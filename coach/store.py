"""Session 存取層。流程程式（flow.py）只透過 SessionStore 介面（new_id / load / save）存取。

- DbSessionStore：正式實作，存在資料庫（本機 SQLite、雲端 Neon Postgres）。
- JsonFileStore：舊的 JSON 檔實作，只留給測試與一次性匯入（scripts/import_json_sessions.py）參考，產品不再使用。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Callable, Protocol, TypeVar

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from coach.db import SessionRow, create_db_engine, utcnow

log = logging.getLogger("coach.store")

_VALID_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
T = TypeVar("T")


class SessionNotFound(LookupError):
    pass


def new_session_id() -> str:
    """128 位元的密碼學隨機值（22 字元）。session id 就是唯一的存取憑證，所以必須無法猜測。"""
    return secrets.token_urlsafe(16)


class SessionStore(Protocol):
    def new_id(self) -> str: ...

    def load(self, session_id: str) -> dict: ...

    def save(self, session: dict) -> None: ...


class DbSessionStore:
    """sessions 資料表。整份 session 存在 payload（JSON／JSONB）。

    資料庫暫時連不上（例如 Neon 剛從休眠醒來）時，自動重試幾次；仍失敗才把例外丟出去。
    """

    RETRY_DELAYS = (0.5, 1.5, 3.0)  # 秒；共最多 4 次嘗試

    def __init__(self, engine: Engine, sleep: Callable[[float], None] = time.sleep) -> None:
        self.engine = engine
        self._sleep = sleep

    def new_id(self) -> str:
        return new_session_id()

    @staticmethod
    def _check(session_id: str) -> str:
        if not _VALID_ID.match(session_id):
            raise SessionNotFound(session_id)
        return session_id

    def _retry(self, fn: Callable[[], T]) -> T:
        for delay in self.RETRY_DELAYS:
            try:
                return fn()
            except OperationalError as e:
                log.warning("資料庫暫時無法使用（%s），%.1f 秒後重試", type(e.orig).__name__, delay)
                self._sleep(delay)
        return fn()

    def load(self, session_id: str) -> dict:
        sid = self._check(session_id)

        def op() -> dict | None:
            with Session(self.engine) as db:
                row = db.get(SessionRow, sid)
                return copy.deepcopy(row.payload) if row else None

        payload = self._retry(op)
        if payload is None:
            raise SessionNotFound(session_id)
        return payload

    def save(self, session: dict) -> None:
        sid = self._check(session["id"])

        def op() -> None:
            with Session(self.engine) as db, db.begin():
                row = db.get(SessionRow, sid)
                if row is None:
                    db.add(SessionRow(id=sid, payload=session))
                else:
                    row.payload = session  # 整個換掉，SQLAlchemy 才偵測得到變更
                    row.updated_at = utcnow()

        try:
            self._retry(op)
        except IntegrityError:
            # 兩個請求同時第一次寫入同一個 id：對方已經插入成功，這次改走更新
            self._retry(op)

    def list_all(self) -> list[dict]:
        """所有 session（最新的在前）。只給命令列工具 chat --list 用。"""

        def op() -> list[dict]:
            with Session(self.engine) as db:
                rows = db.scalars(select(SessionRow).order_by(SessionRow.created_at.desc())).all()
                return [copy.deepcopy(r.payload) for r in rows]

        return self._retry(op)


_default: DbSessionStore | None = None


def default_store() -> DbSessionStore:
    """依環境變數建立（並快取）預設的資料庫 store。第一次呼叫才建立 Engine，所以匯入模組不會連線。"""
    global _default
    if _default is None:
        engine = create_db_engine()
        if engine.dialect.name == "sqlite":
            # 本機 SQLite：自動建好資料表。Postgres 一律由 coach/serve.py 或 alembic upgrade head 負責
            from coach.migrate import upgrade_head

            with engine.begin() as conn:
                upgrade_head(conn)
        _default = DbSessionStore(engine)
    return _default


class DefaultStore:
    """延遲取得 default_store() 的代理：匯入 coach.api 時不建立 Engine、不連線，第一次真的存取才建立。"""

    def new_id(self) -> str:
        return new_session_id()

    def load(self, session_id: str) -> dict:
        return default_store().load(session_id)

    def save(self, session: dict) -> None:
        default_store().save(session)


class JsonFileStore:
    """舊實作：每個對話一個 JSON 檔 outputs/sessions/<session_id>.json。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def new_id(self) -> str:
        return new_session_id()

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
