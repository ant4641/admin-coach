"""一次性匯入：把舊的 outputs/sessions/*.json 寫進資料庫（DATABASE_URL，沒設就是本機 SQLite）。

預設只印出「將匯入幾筆」（dry-run）；加 --apply 才真的寫入。已存在於資料庫的 id 一律略過，不覆蓋。
沒有舊檔或資料夾不存在也不報錯。輸出只有 id 與數量，不印使用者內容。

  python -m scripts.import_json_sessions                    # 看會匯入幾筆
  python -m scripts.import_json_sessions --apply            # 真的匯入
  python -m scripts.import_json_sessions --dir 其他資料夾 --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from coach.store import DbSessionStore, SessionNotFound, default_store  # noqa: E402

DEFAULT_DIR = ROOT / "outputs" / "sessions"


@dataclass
class ImportReport:
    to_import: list[str] = field(default_factory=list)
    already_exist: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)  # 檔名（不是內容）
    imported: int = 0


def run(source: Path, store: DbSessionStore, apply: bool) -> ImportReport:
    report = ImportReport()
    files = sorted(source.glob("*.json")) if source.is_dir() else []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sid = data["id"]
            if not isinstance(sid, str) or sid != path.stem:
                raise ValueError("id 與檔名不一致")
        except (OSError, ValueError, KeyError, TypeError):
            report.unreadable.append(path.name)
            continue
        try:
            store.load(sid)
            report.already_exist.append(sid)
            continue
        except SessionNotFound:
            pass
        report.to_import.append(sid)
        if apply:
            store.save(data)
            report.imported += 1
    return report


def main(argv: list[str] | None = None, store: DbSessionStore | None = None) -> int:
    parser = argparse.ArgumentParser(description="匯入舊的 JSON session 檔到資料庫")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help=f"來源資料夾（預設 {DEFAULT_DIR}）")
    parser.add_argument("--apply", action="store_true", help="真的寫入；沒加只印數量")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(ROOT / ".env")
    rep = run(args.dir, store or default_store(), args.apply)

    print(f"來源：{args.dir}")
    print(f"可匯入：{len(rep.to_import)} 筆；資料庫已存在（略過）：{len(rep.already_exist)} 筆；無法讀取：{len(rep.unreadable)} 筆")
    for name in rep.unreadable:
        print(f"  無法讀取：{name}")
    if args.apply:
        print(f"已寫入：{rep.imported} 筆")
    elif rep.to_import:
        print("這是 dry-run，沒有寫入任何資料。確認後加 --apply 才會匯入。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
