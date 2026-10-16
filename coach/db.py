"""資料庫連線與資料表定義（SQLAlchemy 2.x）。

資料表只能透過 Alembic migration 變更（migrations/）；這裡的 Base.metadata 是 migration 的對照基準。
連線字串來自環境變數 DATABASE_URL（見 coach/settings.py），沒設就用本機 SQLite 檔。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, DateTime, Engine, Index, String, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from coach import settings

CONNECT_TIMEOUT_SECONDS = 15  # Neon 閒置休眠後的第一次連線可能要幾秒
POOL_RECYCLE_SECONDS = 240  # Neon 約 5 分鐘無連線就休眠：在那之前主動換掉舊連線
POOL_TIMEOUT_SECONDS = 30


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 整份 session 內容（輸入、追問紀錄、結果、副產品、負責人、員工名單）。Postgres 用 JSONB
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    # 預留給之後的登入（Google OIDC）。目前一律 NULL，且還沒有 users 表，所以不設外鍵
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("ix_sessions_owner_user_id", "owner_user_id"),)


def create_db_engine(url: str | None = None) -> Engine:
    """建立 Engine。url 省略時讀環境變數。Postgres：連線池、pool_pre_ping、強制 SSL、連線逾時。"""
    parsed = make_url(url or settings.database_url())
    if parsed.get_backend_name() == "sqlite":
        kwargs: dict = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if parsed.database in (None, "", ":memory:"):
            kwargs["poolclass"] = StaticPool  # 記憶體資料庫要所有連線共用同一個
        else:
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(parsed, **kwargs)

    connect_args: dict = {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        # pooled（pgbouncer transaction 模式）連線不支援 prepared statement 自動快取
        "prepare_threshold": None,
    }
    if "sslmode" not in parsed.query:
        connect_args["sslmode"] = "require"
    return create_engine(
        parsed,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=POOL_RECYCLE_SECONDS,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        connect_args=connect_args,
    )
