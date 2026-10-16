"""雲端啟動入口：python -m coach.serve

順序：讀 .env（有的話）→ 檢查必要環境變數 → 套用 Alembic migration → 啟動 uvicorn（0.0.0.0:$PORT）。
本機仍用 run.bat／uvicorn，不經過這裡。環境變數 RUN_MIGRATIONS=0 可跳過 migration。
"""

from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

from coach import settings

log = logging.getLogger("coach.serve")


def main() -> int:
    load_dotenv(settings.ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings.validate()
    except settings.SettingsError as e:
        print(f"[設定錯誤] {e}", file=sys.stderr)
        return 1

    if os.getenv("RUN_MIGRATIONS", "1").strip() != "0":
        from coach.migrate import upgrade_head

        try:
            upgrade_head()
        except Exception as e:  # 連線字串不會出現在例外類型名稱裡；訊息可能含主機名，所以只記類型
            print(f"[資料庫錯誤] 套用 migration 失敗（{type(e).__name__}）。請檢查 DATABASE_URL 與 Neon 狀態。", file=sys.stderr)
            return 1
        log.info("migration 已是最新版")

    import uvicorn

    uvicorn.run(
        "coach.api:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=False,  # 來源 IP 由 coach/ratelimit.py 自己判斷，不讓 uvicorn 改寫
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
