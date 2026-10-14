"""命令列互動：不開網頁就能跑完整流程（和 API 共用 coach/flow.py）。

python -m coach.chat                                          # 新對話，依序輸入稱呼 / 理想情境 / 現況
python -m coach.chat --case test_cases/01_design_company.md   # 稱呼 / 理想 / 現況取自案例檔
python -m coach.chat --resume <session_id>                    # 接續之前的對話
python -m coach.chat --resume <session_id> --weekly 週回報.md   # 進入執行後，週回報取自檔案
python -m coach.chat --list                                   # 列出所有 session

輸入欄位時，開頭打 @ 表示從檔案讀（例如 @ideal.txt），長文字用這個。Ctrl+C 離開，之後用 --resume 接續。
週回報檔用「## 實際狀況 / ## 本週成果分數 / ## 哪些達成 / ## 哪些沒達成 / ## 原因」分段（週回報測試案例檔也可以直接拿來用）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from coach.cases import load_case, parse_sections
from coach.flow import CONFIDENCE_THRESHOLD, CoachService, FlowError, Outcome
from coach.llm import get_llm
from coach.store import JsonFileStore, SessionNotFound

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / "outputs" / "sessions"

InputFn = Callable[[str], str]


def _read(prompt: str, input_fn: InputFn, required: bool = True) -> str:
    while True:
        text = input_fn(f"{prompt}：").strip()
        if text.startswith("@"):
            path = Path(text[1:].strip().strip('"'))
            if not path.is_file():
                print(f"找不到檔案：{path}")
                continue
            text = path.read_text(encoding="utf-8").strip()
        if text or not required:
            return text
        print("不可空白")


def _read_score(prompt: str, input_fn: InputFn) -> int:
    while True:
        text = input_fn(f"{prompt}（0～100）：").strip()
        if text.isdigit() and 0 <= int(text) <= 100:
            return int(text)
        print("請輸入 0～100 的整數")


def _print_outcome(o: Outcome) -> None:
    print()
    tag = {"clarify": "追問", "plan": f"等級表 第 {o.version} 版", "ready": "進入執行"}.get(o.status, o.status)
    print(f"━━ {tag} ━━")
    print(o.message)
    if o.advice:
        print(f"\n【建議】{o.advice}")
    if o.changes:
        print("\n【與上一版相比改了哪裡】")
        for c in o.changes:
            print(f"- [{c.get('type', '?')}] 等級 {c.get('level', '?')} {c.get('name', '')}：{c.get('reason', '')}")
            if c.get("before"):
                print(f"    原：{c['before']}")
            if c.get("after"):
                print(f"    新：{c['after']}")
    if o.diff:
        print("\n【逐行差異】")
        print(o.diff)
    if o.analysis:
        print("\n【週回報分析】")
        for label, key in (("達成／正面", "positive"), ("沒達成／負面", "negative"), ("中立", "neutral")):
            for line in o.analysis.get(key) or []:
                print(f"- {label}：{line}")
        for it in o.analysis.get("items") or []:
            print(f"- {it.get('type', '?')}（{it.get('action', '?')}）：{it.get('item', '')}")
    print()


def _print_state(s: dict) -> None:
    """接續舊對話時，先顯示目前停在哪裡。"""
    print(f"session {s['id']}（{s['name']}）狀態：{s['status']}")
    if s["status"] == "clarify":
        print(f"\n{s['pending']['question']}\n")
    elif s["status"] == "plan":
        print(f"\n目前是第 {s['versions'][-1]['no']} 版，等你給信心分數。\n\n{s['versions'][-1]['table']}\n")


def _list_sessions(store: JsonFileStore) -> None:
    import json

    for p in sorted(store.directory.glob("*.json")) if store.directory.exists() else []:
        s = json.loads(p.read_text(encoding="utf-8"))
        print(f"{s['id']}  {s['status']:8}  版本 {len(s['versions'])}  週回報 {len(s['weekly_reports'])}  {s['name']}")


def _weekly_fields(weekly_file: Path | None, input_fn: InputFn) -> dict:
    if weekly_file:
        sec = parse_sections(weekly_file.read_text(encoding="utf-8"))
        score = sec.get("本週成果分數", "")
        if not sec.get("實際狀況") or not score.isdigit():
            raise FlowError(f"{weekly_file.name} 缺少「## 實際狀況」或「## 本週成果分數」（整數）")
        return {
            "actual": sec["實際狀況"],
            "score": int(score),
            "achieved": sec.get("哪些達成", ""),
            "not_achieved": sec.get("哪些沒達成", ""),
            "reason": sec.get("原因", ""),
        }
    return {
        "actual": _read("實際狀況", input_fn),
        "score": _read_score("本週成果分數", input_fn),
        "achieved": _read("哪些達成（可空白）", input_fn, required=False),
        "not_achieved": _read("哪些沒達成（可空白）", input_fn, required=False),
        "reason": _read("原因（可空白）", input_fn, required=False),
    }


def run_loop(svc: CoachService, session_id: str, input_fn: InputFn, weekly_file: Path | None = None) -> int:
    """依 session 目前的狀態決定下一步要問什麼，直到使用者離開。"""
    while True:
        s = svc.get(session_id)
        status = s["status"]
        if status == "clarify":
            out = svc.answer(session_id, _read("你的回答", input_fn))
        elif status == "plan":
            out = svc.confidence(session_id, _read_score(f"對這一版的執行信心分數（>= {CONFIDENCE_THRESHOLD} 才進入執行）", input_fn))
        else:  # ready
            if weekly_file is None:
                if input_fn("現在要輸入週回報嗎？（y / 其他鍵離開）：").strip().lower() != "y":
                    print(f"之後用 --resume {session_id} 接續。")
                    return 0
            out = svc.weekly_report(session_id, **_weekly_fields(weekly_file, input_fn))
            weekly_file = None  # 檔案只用一次，後續（新版本重新給分之後）改用互動輸入
        _print_outcome(out)


def main(argv: list[str] | None = None, input_fn: InputFn = input, service: CoachService | None = None) -> int:
    parser = argparse.ArgumentParser(description="管到飛：命令列互動教練")
    parser.add_argument("--case", type=Path, help="從案例檔讀稱呼 / 理想情境 / 現在情境")
    parser.add_argument("--resume", metavar="SESSION_ID", help="接續之前的對話")
    parser.add_argument("--weekly", type=Path, metavar="FILE", help="進入執行後，用這個檔案當週回報")
    parser.add_argument("--list", action="store_true", help="列出所有 session")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    store = JsonFileStore(SESSIONS_DIR)
    if args.list:
        _list_sessions(store)
        return 0
    svc = service or CoachService(store, get_llm)

    session_id = args.resume
    try:
        if args.resume:
            _print_state(svc.get(session_id))
        else:
            if args.case:
                name, ideal, current = load_case(args.case).intake_fields()
            else:
                name = _read("稱呼（例如 林主管）", input_fn)
                ideal = _read("理想情境（像電影畫面的具體場景）", input_fn)
                current = _read("現在情境（團隊人數與職位、目前怎麼運作、最常處理的三個狀況）", input_fn)
            out = svc.start(name, ideal, current)
            session_id = out.session_id
            _print_outcome(out)
        return run_loop(svc, session_id, input_fn, args.weekly)
    except SessionNotFound as e:
        print(f"找不到 session：{e}")
        return 1
    except FlowError as e:
        print(f"\n失敗（{type(e).__name__}）：{e}")
        if session_id:
            print(f"這一步沒有被存檔，session 維持原樣；用 --resume {session_id} 重新接續即可。")
        return 1
    except KeyError as e:
        print(f"案例檔缺少段落：{e}（需要 ## 稱呼 / ## 理想情境 / ## 現在情境）")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\n已離開。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
