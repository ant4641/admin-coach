"""程式內套用 Alembic migration（等同 alembic upgrade head）。

- 雲端：coach/serve.py 啟動前呼叫（連 DATABASE_URL_DIRECT，沒有就連 DATABASE_URL）。
- 本機 SQLite：default_store() 第一次建立時自動呼叫，雙擊 run.bat 不用另外跑指令。
"""

from __future__ import annotations

from alembic import command
from alembic.config import Config

from coach.settings import ROOT


def upgrade_head(connection=None) -> None:
    """套用到最新版。connection 省略時，env.py 依環境變數自己連線。"""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["skip_logging"] = True  # 不要覆蓋 uvicorn 的日誌設定
    if connection is not None:
        cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")
