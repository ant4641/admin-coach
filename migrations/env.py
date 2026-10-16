"""Alembic 環境。連線字串從環境變數來（coach/settings.py），不寫在 alembic.ini。

用法：alembic upgrade head ／ alembic revision --autogenerate -m "說明" ／ alembic current
程式內呼叫（coach/serve.py、測試）可在 config.attributes 放 "connection" 指定連線。
"""

from logging.config import fileConfig

from alembic import context

from coach import settings
from coach.db import Base, create_db_engine

config = context.config
if config.config_file_name is not None and not config.attributes.get("skip_logging"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """離線模式：只輸出 SQL，不連線（alembic upgrade head --sql）。"""
    context.configure(
        url=settings.migration_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = create_db_engine(settings.migration_database_url())
    try:
        with engine.connect() as conn:
            _run(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
