"""測試案例執行器：讀規則檔 + 案例 → 多輪呼叫 LLM → 寫輸出檔與 runs.csv。

python -m coach.cli --all
python -m coach.cli test_cases/01_design_vague.md
python -m coach.cli --all --dry-run

案例的 answers 依序回答追問；沒有 answers 就只跑第一輪。
安全上限：一個案例最多呼叫模型 MAX_ROUNDS 次，超過標記失敗（over_limit），只用來避免失控花費。
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from coach.cases import Case, load_case, load_cases
from coach.display import render_table
from coach.flow import CoachService, FlowError, Outcome
from coach.inputs import build_inputs, parse_industries
from coach.llm import LLMClient, get_llm
from coach.prompt import build_messages
from coach.rules import DEFAULT_RULES_PATH, Rules, load_rules
from coach.store import JsonFileStore

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "test_cases"
OUTPUTS_DIR = ROOT / "outputs"
DRY_RUN_DIRNAME = "_dry_run"  # outputs/_dry_run/，每次執行會先清空
MAX_ROUNDS = 10

CSV_FIELDS = [
    "generated_at",
    "case",
    "rules_version",
    "rules_sha8",
    "provider",
    "model",
    "temperature",
    "expected_status",
    "actual_status",
    "match",
    "rounds",
    "byproducts",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "session_id",
    "output_file",
]


@dataclass
class CaseRun:
    status: str  # clarify | vfp | over_limit | error
    outcome: Outcome | None = None
    session_id: str = ""
    rounds: int = 0  # 呼叫模型的次數
    answers_used: int = 0
    answers_total: int = 0
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    model: str = ""


def run_case(case: Case, rules: Rules, llm: LLMClient) -> CaseRun:
    svc = CoachService(JsonFileStore(OUTPUTS_DIR / "sessions"), lambda: llm, lambda: rules)
    answers = case.answers()
    run = CaseRun(status="error", answers_total=len(answers))

    def track(out: Outcome) -> None:
        run.outcome, run.session_id = out, out.session_id
        run.rounds += 1
        if out.llm_result:
            run.model = out.llm_result.model
            run.latency_ms += out.llm_result.latency_ms
            for k, v in out.llm_result.usage.items():
                run.usage[k] = run.usage.get(k, 0) + v

    try:
        track(svc.start(**case.start_fields()))
        run.status = run.outcome.status
        while run.status == "clarify" and run.answers_used < len(answers):
            if run.rounds >= MAX_ROUNDS:
                run.status = "over_limit"
                break
            track(svc.answer(run.session_id, answers[run.answers_used]))
            run.answers_used += 1
            run.status = run.outcome.status
    except FlowError as e:
        run.status, run.error = "error", f"{type(e).__name__}: {e}"
    return run


def render_output(case: Case, rules: Rules, llm: LLMClient, run: CaseRun, generated_at: str) -> str:
    meta = {
        "case": case.name,
        "case_title": case.meta.get("title", ""),
        "case_sha256": case.sha256,
        "rules_file": rules.path.name,
        "rules_version": rules.version,
        "rules_version_date": rules.version_date,
        "rules_sha256": rules.sha256,
        "provider": llm.provider,
        "model": run.model,
        "temperature": getattr(llm, "temperature", None),
        "generated_at": generated_at,
        "session_id": run.session_id,
        "rounds": run.rounds,
        "answers_used": f"{run.answers_used}/{run.answers_total}",
        **run.usage,
        "expected_status": case.expected_status,
        "actual_status": run.status,
        "match": is_match(case, run),
    }
    header = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items())
    parts = [f"---\n{header}\n---", f"# {case.meta.get('title', case.name)}", "## 輸入", case.body]
    if run.session_id:
        s = JsonFileStore(OUTPUTS_DIR / "sessions").load(run.session_id)
        lines = []
        for ex in s["exchanges"]:
            lines.append(f"教練：{ex['coach']['question']}")
            if ex.get("manager") is not None:
                lines.append(f"主管：{ex['manager']}")
        if lines:
            parts += ["## 對話", "\n\n".join(lines)]
    if run.outcome and run.outcome.status == "vfp":
        parts += ["## 結果", "```\n" + render_table(run.outcome.vfp, run.outcome.byproducts) + "\n```"]
    if run.status == "over_limit":
        parts += ["## ⚠ 超過安全上限", f"呼叫模型已達 {MAX_ROUNDS} 次仍在追問，停止。"]
    if run.error:
        parts += ["## ⚠ 失敗", f"`{run.error}`"]
    return "\n\n".join(parts) + "\n"


def render_dry_run(case: Case, rules: Rules) -> str:
    """第一輪會送給模型的內容。規則檔原文太長且固定，只註明來源。"""
    inputs = build_inputs(parse_industries(rules.text), **case.start_fields())
    system, user = build_messages(rules, inputs, [])
    return "\n\n".join(
        [
            f"# [dry-run] {case.meta.get('title', case.name)}",
            f"- 規則檔：{rules.path.name}　版本 {rules.version}　sha {rules.sha256[:8]}",
            f"- 預期 status：{case.expected_status}　answers：{len(case.answers())} 個",
            f"## 1. system：規則檔原文（略，共 {len(system['content'])} 字）",
            "## 2. user：輸入",
            user["content"],
        ]
    ) + "\n"


def is_match(case: Case, run: CaseRun) -> bool:
    return case.expected_status is None or case.expected_status == run.status


def append_csv(row: dict) -> None:
    path = OUTPUTS_DIR / "runs.csv"
    is_new = not path.exists()
    # utf-8-sig：Excel 開啟中文不會亂碼
    with path.open("a", newline="", encoding="utf-8-sig" if is_new else "utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def execute(case: Case, rules: Rules, llm: LLMClient) -> CaseRun:
    run = run_case(case, rules, llm)
    now = datetime.now().astimezone()
    generated_at = now.isoformat(timespec="seconds")
    out_dir = OUTPUTS_DIR / case.name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{now:%Y%m%d-%H%M%S}__rules-{rules.tag}__{llm.provider}.md"
    out_file.write_text(render_output(case, rules, llm, run, generated_at), encoding="utf-8")
    match = is_match(case, run)
    append_csv(
        {
            "generated_at": generated_at,
            "case": case.name,
            "rules_version": rules.version,
            "rules_sha8": rules.sha256[:8],
            "provider": llm.provider,
            "model": run.model,
            "temperature": getattr(llm, "temperature", None),
            "expected_status": case.expected_status or "",
            "actual_status": run.status,
            "match": match,
            "rounds": run.rounds,
            "byproducts": len(run.outcome.byproducts) if run.outcome else "",
            "latency_ms": run.latency_ms,
            "prompt_tokens": run.usage.get("prompt_tokens", ""),
            "completion_tokens": run.usage.get("completion_tokens", ""),
            "session_id": run.session_id,
            "output_file": out_file.relative_to(ROOT).as_posix(),
        }
    )
    print(
        f"[{'OK  ' if match else 'FAIL'}] {case.name}: expected={case.expected_status} actual={run.status} "
        f"rounds={run.rounds} → {out_file.relative_to(ROOT)}"
    )
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="管到飛：用規則檔跑測試案例")
    parser.add_argument("cases", nargs="*", type=Path, help="案例檔路徑")
    parser.add_argument("--all", action="store_true", help=f"跑 {CASES_DIR.name}/ 下所有案例")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH, help="規則檔路徑")
    parser.add_argument("--provider", help="覆寫 LLM_PROVIDER（azure / openai）")
    parser.add_argument("--dry-run", action="store_true", help=f"不呼叫 LLM，把第一輪 messages 寫到 outputs/{DRY_RUN_DIRNAME}/")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cases = load_cases(CASES_DIR) if args.all else [load_case(p) for p in args.cases]
    if not cases:
        parser.error("請指定案例檔，或加 --all")
    rules = load_rules(args.rules)
    print(f"規則檔 {rules.path.name}  版本 {rules.version} ({rules.version_date})  sha {rules.sha256[:8]}")

    todo = [c for c in cases if c.todo]
    for c in todo:
        print(f"[SKIP] {c.name}: {c.todo}")
    cases = [c for c in cases if not c.todo]

    if args.dry_run:
        out_dir = OUTPUTS_DIR / DRY_RUN_DIRNAME
        shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True)
        for case in cases:
            out_file = out_dir / f"{case.name}.md"
            out_file.write_text(render_dry_run(case, rules), encoding="utf-8")
            print(f"{case.name} → {out_file.relative_to(ROOT)}")
        return 0

    llm = get_llm(args.provider)
    failed = sum(not is_match(c, execute(c, rules, llm)) for c in cases)
    print(f"\n{len(cases) - failed}/{len(cases)} 符合預期" + (f"（{len(todo)} 個待補略過）" if todo else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
