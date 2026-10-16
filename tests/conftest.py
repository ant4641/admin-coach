"""所有測試共用：資料庫一律指到暫存 SQLite，絕不碰 .env 的 DATABASE_URL 或 outputs/coach.db。"""

from __future__ import annotations

import pytest

from coach import store


@pytest.fixture(autouse=True)
def _isolated_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'default_test.db').as_posix()}")
    monkeypatch.delenv("DATABASE_URL_DIRECT", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    monkeypatch.setattr(store, "_default", None)
