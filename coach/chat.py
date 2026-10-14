"""命令列互動：不開網頁就能跑完整流程（和 API 共用 coach/flow.py）。

python -m coach.chat                                          # 新對話，依序回答六個問題
python -m coach.chat --case test_cases/01_design_vague.md     # 六項輸入取自案例檔
python -m coach.chat --resume <session_id>                    # 接續之前的對話
python -m coach.chat --list                                   # 列出所有 session

輸入欄位時，開頭打 @ 表示從檔案讀。Ctrl+C 離開，之後用 --resume 接續。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from coach.cases import load_case
from coach.display import render_table
from coach.flow import CoachService, FlowError, Outcome, current_result
from coach.inputs import Q_INDUSTRY, Q_NAME, Q_PRODUCT, Q_REASON, Q_SALES, Q_SCORE, is_other, parse_industries
from coach.llm import get_llm
from coach.rules import load_rules
from coach.store import JsonFileStore, SessionNotFound

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / "outputs" / "sessions"

InputFn = Callable[[str], str]


def _read(prompt: str, input_fn: InputFn, required: bool = True) -> str:
    while True:
        text = input_fn(f"{prompt} ").strip()
        if text.startswith("@"):
            path = Path(text[1:].strip().strip('"'))
            if not path.is_file():
                print(f"找不到檔案：{path}")
                continue
            text = path.read_text(encoding="utf-8").strip()
        if text or not required:
            return text
        print("不可空白")


def _read_number(prompt: str, input_fn: InputFn, lo: int, hi: int, required: bool) -> int | None:
    while True:
        text = input_fn(f"{prompt} ").strip()
        if not text and not required:
            return None
        if text.isdigit() and lo <= int(text) <= hi:
            return int(text)
        print(f"請輸入 {lo}～{hi} 的整數")


def collect_inputs(industries: list[str], input_fn: InputFn) -> dict:
    """依序問六題，回傳 CoachService.start() 的參數。只有稱呼和產業必填。"""
    name = _read(f"{Q_NAME}", input_fn)
    print(f"\n{Q_INDUSTRY}")
    for i, item in enumerate(industries, start=1):
        print(f"  {i}. {item}")
    no = _read_number("請輸入編號：", input_fn, 1, len(industries), required=True)
    other = _read("請填寫產業：", input_fn) if is_other(industries[no - 1]) else ""
    product = _read(f"{Q_PRODUCT}（可空白）", input_fn, required=False)
    sales = _read(f"{Q_SALES}（可空白）", input_fn, required=False)
    score = _read_number(f"{Q_SCORE}（可空白）", input_fn, 1, 10, required=False)
    reason = _read(f"{Q_REASON}（可空白）", input_fn, required=False)
    return dict(
        name=name, industry_no=no, industry_other=other, product=product, sales=sales, score=score, reason=reason
    )


def _print_outcome(o: Outcome) -> None:
    print()
    if o.status == "clarify":
        print(f"教練：{o.question}")
    else:
        print("━━ 最終有價值產品與副產品 ━━")
        print(render_table(o.vfp, o.byproducts))
    print()


def _print_state(s: dict) -> None:
    """接續舊對話時，先顯示目前停在哪裡。"""
    print(f"session {s['id']}（{s['inputs']['name']}）狀態：{s['status']}\n")
    if s["status"] == "clarify":
        print(f"教練：{s['pending']['question']}\n")
    else:
        cur = current_result(s)
        print(render_table(cur["vfp"], cur["byproducts"]) + "\n")


def _list_sessions(store: JsonFileStore) -> None:
    for p in sorted(store.directory.glob("*.json")) if store.directory.exists() else []:
        s = json.loads(p.read_text(encoding="utf-8"))
        print(f"{s['id']}  {s['status']:8}  {s['inputs']['name']}")


def run_loop(svc: CoachService, session_id: str, input_fn: InputFn) -> int:
    """clarify 就問使用者並送出回答，直到 vfp。"""
    while svc.get(session_id)["status"] == "clarify":
        out = svc.answer(session_id, _read("你的回答：", input_fn))
        _print_outcome(out)
    return 0


def main(argv: list[str] | None = None, input_fn: InputFn = input, service: CoachService | None = None) -> int:
    parser = argparse.ArgumentParser(description="管到飛：命令列互動教練")
    parser.add_argument("--case", type=Path, help="從案例檔讀六項輸入")
    parser.add_argument("--resume", metavar="SESSION_ID", help="接續之前的對話")
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
                case = load_case(args.case)
                if case.todo:
                    print(f"{case.name} 尚未提供內容（{case.todo}）")
                    return 1
                fields = case.start_fields()
            else:
                fields = collect_inputs(parse_industries(load_rules().text), input_fn)
            out = svc.start(**fields)
            session_id = out.session_id
            _print_outcome(out)
        return run_loop(svc, session_id, input_fn)
    except SessionNotFound as e:
        print(f"找不到 session：{e}")
        return 1
    except FlowError as e:
        print(f"\n失敗（{type(e).__name__}）：{e}")
        if session_id:
            print(f"這一步沒有被存檔，session 維持原樣；用 --resume {session_id} 重新接續即可。")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\n已離開。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
