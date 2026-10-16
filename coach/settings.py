"""設定集中管理：所有環境變數的讀取與啟動檢查都在這裡。

本機：不設 APP_ENV，缺值時照舊（模型缺金鑰在呼叫時回 503，資料庫預設 SQLite），不強制檢查。
雲端：APP_ENV=production，啟動時檢查必要變數，缺什麼就列出「變數名稱」後結束（絕不印出值）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE_PATH = ROOT / "outputs" / "coach.db"

# 每個 LLM_PROVIDER 在雲端必須有的變數（名稱 → 用途）
_REQUIRED_LLM: dict[str, dict[str, str]] = {
    "azure": {
        "AZURE_OPENAI_ENDPOINT": "Azure OpenAI 端點網址",
        "AZURE_OPENAI_API_KEY": "Azure OpenAI 金鑰",
        "AZURE_OPENAI_DEPLOYMENT": "Azure 上的部署名稱",
    },
    "openai": {"OPENAI_API_KEY": "OpenAI 金鑰"},
}


class SettingsError(RuntimeError):
    """設定缺漏或格式錯誤。訊息只含變數名稱，不含值。"""


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def is_production(env: Mapping[str, str] | None = None) -> bool:
    return _env(env).get("APP_ENV", "").strip().lower() == "production"


def database_url(env: Mapping[str, str] | None = None) -> str:
    """SQLAlchemy 連線字串。DATABASE_URL 有設就用（Neon Postgres，驅動固定 psycopg 3）；沒設用本機 SQLite 檔。"""
    raw = _env(env).get("DATABASE_URL", "").strip()
    if not raw:
        return f"sqlite:///{DEFAULT_SQLITE_PATH.as_posix()}"
    # Neon／Heroku 風格的 postgres:// 與 postgresql:// 都轉成 psycopg 3 的方言
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+psycopg://" + raw.removeprefix(prefix)
    return raw


def migration_database_url(env: Mapping[str, str] | None = None) -> str:
    """跑 migration 用的連線字串：有 DATABASE_URL_DIRECT 就用它（Neon 非 pooled），否則同 DATABASE_URL。"""
    direct = _env(env).get("DATABASE_URL_DIRECT", "").strip()
    if direct:
        return database_url({"DATABASE_URL": direct})
    return database_url(env)


def trusted_proxy_hops(env: Mapping[str, str] | None = None) -> int | None:
    """前面有幾層會「附加來源位址」到 X-Forwarded-For 的代理。None＝沿用本機／ngrok 的寬鬆判斷。

    雲端（production）沒設時預設 1：只信 X-Forwarded-For 最右邊那一個（Render 代理附加的，用戶端偽造不了）。
    """
    e = _env(env)
    raw = e.get("TRUSTED_PROXY_HOPS", "").strip()
    if raw:
        try:
            n = int(raw)
        except ValueError as exc:
            raise SettingsError("TRUSTED_PROXY_HOPS 必須是整數") from exc
        if n < 0:
            raise SettingsError("TRUSTED_PROXY_HOPS 不可為負數")
        return n or None
    return 1 if is_production(e) else None


def missing_variables(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """雲端部署必要但沒設的變數（名稱 → 用途）。"""
    e = _env(env)
    need: dict[str, str] = {"DATABASE_URL": "Neon Postgres 連線字串"}
    provider = e.get("LLM_PROVIDER", "azure").strip().lower() or "azure"
    need.update(_REQUIRED_LLM.get(provider, {}))
    return {name: why for name, why in need.items() if not e.get(name, "").strip()}


def validate(env: Mapping[str, str] | None = None) -> None:
    """雲端（production）才檢查；本機直接通過。有問題丟 SettingsError，訊息為繁體中文且不含任何值。"""
    e = _env(env)
    if not is_production(e):
        return
    provider = e.get("LLM_PROVIDER", "azure").strip().lower() or "azure"
    if provider not in _REQUIRED_LLM:
        raise SettingsError(f"LLM_PROVIDER 不支援：{provider}（可用：{', '.join(_REQUIRED_LLM)}）")
    missing = missing_variables(e)
    if missing:
        lines = "\n".join(f"  - {name}（{why}）" for name, why in missing.items())
        raise SettingsError(f"缺少必要的環境變數，請到 Render 的 Environment 設定：\n{lines}")
    if database_url(e).startswith("sqlite"):
        raise SettingsError("雲端環境的 DATABASE_URL 必須是 Postgres 連線字串，不可使用 SQLite（重新部署會清空檔案）")
    trusted_proxy_hops(e)  # 格式錯誤時在這裡就報錯
