"""命令列：讀規則檔 + 測試案例 → 呼叫 LLM → 寫輸出檔。

python -m coach.cli --all
python -m coach.cli test_cases/01_noodle_shop.md
python -m coach.cli --all --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from coach.cases import Case, load_case, load_cases
from coach.flow import CoachService, FlowError, Outcome
from coach.llm import LLMClient, LLMResult, get_llm
from coach.prompt import WEEKLY_CONTRACT, CoachReply, build_messages, parse_reply, weekly_context
from coach.rules import DEFAULT_RULES_PATH, Rules, load_rules
from coach.store import JsonFileStore

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "test_cases"
OUTPUTS_DIR = ROOT / "outputs"
DRY_RUN_DIRNAME = "_dry_run"  # outputs/_dry_run/，每次執行會先清空，可隨時整個刪除

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
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "output_file",
]


def render_output(
    case: Case, rules: Rules, llm: LLMClient, result: LLMResult, reply: CoachReply, generated_at: str
) -> str:
    meta = {
        "case": case.name,
        "case_title": case.meta.get("title", ""),
        "case_sha256": case.sha256,
        "rules_file": rules.path.name,
        "rules_version": rules.version,
        "rules_version_date": rules.version_date,
        "rules_sha256": rules.sha256,
        "rules_mtime": rules.mtime,
        "provider": llm.provider,
        "model": result.model,
        "temperature": getattr(llm, "temperature", None),
        "generated_at": generated_at,
        "latency_ms": result.latency_ms,
        **result.usage,
        "expected_status": case.expected_status,
        "actual_status": reply.status,
        "match": case.expected_status is None or case.expected_status == reply.status,
        "missing": reply.missing,
    }
    header = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items())
    parts = [f"---\n{header}\n---", f"# {case.meta.get('title', case.name)}", "## 輸入", case.body, "## 教練回覆", reply.message]
    if reply.parse_error:
        parts += ["## ⚠ 解析失敗", f"`{reply.parse_error}`", "原始回覆：", f"```\n{result.text}\n```"]
    return "\n\n".join(parts) + "\n"


def render_weekly_dry_run(case: Case, rules: Rules) -> str:
    sec = case.sections
    if "上週等級表" in sec:
        name, ideal, current, table = sec["稱呼"], sec["理想情境"], sec["現在情境"], sec["上週等級表"]
    else:
        base = load_case(CASES_DIR / f"{case.meta['base_case']}.md")
        (name, ideal, current), table = base.intake_fields(), f"（執行時由 {case.meta['base_case']} 案例呼叫模型產出，再以信心分數 {case.meta['confidence']} 進入執行）"
    report = {**case.weekly_fields()}
    return "\n\n".join(
        [
            f"# [dry-run] {case.meta.get('title', case.name)}",
            f"- 規則檔：{rules.path.name}　版本 {rules.version}　sha {rules.sha256[:8]}",
            f"- 預期週回報結果 status：{case.expected_status}",
            "## 1. system：規則檔原文（略）",
            "## 2. system：輸出約定（程式附加）：基本約定 + 週回報階段約定",
            WEEKLY_CONTRACT,
            "## 3. user：週回報情境",
            weekly_context(name, ideal, current, 1, table, report, []),
        ]
    ) + "\n"


def render_dry_run(case: Case, rules: Rules) -> str:
    """會送給模型的內容，排成好讀的 Markdown。規則檔原文太長且固定，只註明來源。"""
    if case.kind == "weekly":
        return render_weekly_dry_run(case, rules)
    system_rules, contract, user = build_messages(rules, case.body)
    return "\n\n".join(
        [
            f"# [dry-run] {case.meta.get('title', case.name)}",
            f"- 規則檔：{rules.path.name}　版本 {rules.version}　sha {rules.sha256[:8]}",
            f"- 預期 status：{case.expected_status}",
            "## 1. system：規則檔原文（略，共 "
            f"{len(system_rules['content'])} 字，內容即 {rules.path.name}）",
            "## 2. system：輸出約定（程式附加）",
            contract["content"],
            "## 3. user：案例輸入",
            user["content"],
        ]
    ) + "\n"


def append_csv(row: dict) -> None:
    path = OUTPUTS_DIR / "runs.csv"
    is_new = not path.exists()
    # utf-8-sig：Excel 開啟中文不會亂碼
    with path.open("a", newline="", encoding="utf-8-sig" if is_new else "utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def _log_run(
    case: Case, rules: Rules, llm: LLMClient, reply: CoachReply, result: LLMResult | None, out_file: Path, generated_at: str
) -> None:
    match = case.expected_status is None or case.expected_status == reply.status
    usage = result.usage if result else {}
    append_csv(
        {
            "generated_at": generated_at,
            "case": case.name,
            "rules_version": rules.version,
            "rules_sha8": rules.sha256[:8],
            "provider": llm.provider,
            "model": result.model if result else "",
            "temperature": getattr(llm, "temperature", None),
            "expected_status": case.expected_status or "",
            "actual_status": reply.status,
            "match": match,
            "latency_ms": result.latency_ms if result else "",
            "prompt_tokens": usage.get("prompt_tokens", ""),
            "completion_tokens": usage.get("completion_tokens", ""),
            "output_file": out_file.relative_to(ROOT).as_posix(),
        }
    )
    flag = "OK  " if match else "FAIL"
    print(f"[{flag}] {case.name}: expected={case.expected_status} actual={reply.status} → {out_file.relative_to(ROOT)}")


def _out_file(case: Case, rules: Rules, llm: LLMClient, now: datetime) -> Path:
    out_dir = OUTPUTS_DIR / case.name
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{now:%Y%m%d-%H%M%S}__rules-{rules.tag}__{llm.provider}.md"


def run_case(case: Case, rules: Rules, llm: LLMClient) -> CoachReply:
    if case.kind == "weekly":
        return run_weekly_case(case, rules, llm)
    result = llm.complete(build_messages(rules, case.body))
    reply = parse_reply(result.text)

    now = datetime.now().astimezone()
    generated_at = now.isoformat(timespec="seconds")
    out_file = _out_file(case, rules, llm, now)
    out_file.write_text(render_output(case, rules, llm, result, reply, generated_at), encoding="utf-8")
    _log_run(case, rules, llm, reply, result, out_file, generated_at)
    return reply


def run_weekly_case(case: Case, rules: Rules, llm: LLMClient) -> CoachReply:
    """週回報案例：建立「已進入執行」的 session → 送週回報 → 看結果 status。

    實際 status 以「週回報這一步」的結果為準；若第一版等級表就沒產出（base_case 回 clarify），
    記成 base_clarify，一定不符合預期，避免誤判通過。
    """
    svc = CoachService(JsonFileStore(OUTPUTS_DIR / "sessions"), lambda: llm, lambda: rules)
    sec = case.sections
    confidence = int(case.meta["confidence"])
    session_id = ""
    out: Outcome | None = None
    try:
        if "上週等級表" in sec:
            seeded = svc.seed(sec["稱呼"], sec["理想情境"], sec["現在情境"], sec["上週等級表"], confidence)
        else:
            base = svc.start(*load_case(CASES_DIR / f"{case.meta['base_case']}.md").intake_fields())
            session_id = base.session_id
            if base.status != "plan":
                reply = CoachReply(status=f"base_{base.status}", message=base.message)
            seeded = svc.confidence(session_id, confidence) if base.status == "plan" else None
        if seeded is not None:
            session_id = seeded.session_id
            out = svc.weekly_report(session_id, **case.weekly_fields())
            reply = CoachReply(status=out.status, message=out.message, missing=out.missing)
    except FlowError as e:
        reply = CoachReply(status="unknown", message=str(e), parse_error=f"{type(e).__name__}: {e}")

    now = datetime.now().astimezone()
    generated_at = now.isoformat(timespec="seconds")
    out_file = _out_file(case, rules, llm, now)
    out_file.write_text(render_weekly_output(case, rules, llm, session_id, out, reply, generated_at), encoding="utf-8")
    _log_run(case, rules, llm, reply, out.llm_result if out else None, out_file, generated_at)
    return reply


def render_weekly_output(
    case: Case, rules: Rules, llm: LLMClient, session_id: str, out: Outcome | None, reply: CoachReply, generated_at: str
) -> str:
    meta = {
        "case": case.name,
        "case_title": case.meta.get("title", ""),
        "case_sha256": case.sha256,
        "rules_file": rules.path.name,
        "rules_version": rules.version,
        "rules_sha256": rules.sha256,
        "provider": llm.provider,
        "generated_at": generated_at,
        "session_id": session_id,
        "confidence_to_ready": case.meta.get("confidence"),
        "expected_status": case.expected_status,
        "actual_status": reply.status,
        "match": case.expected_status is None or case.expected_status == reply.status,
    }
    header = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items())
    def fenced(lang: str, body: str) -> str:
        return f"```{lang}\n{body}\n```"

    parts = [f"---\n{header}\n---", f"# {case.meta.get('title', case.name)}", "## 輸入", case.body, "## 教練回覆", reply.message]
    if out and out.changes:
        parts += ["## 與上週相比改了哪裡（模型說明）", fenced("json", json.dumps(out.changes, ensure_ascii=False, indent=2))]
    if out and out.diff:
        parts += ["## 逐行差異（程式計算）", fenced("diff", out.diff)]
    if out and out.analysis:
        parts += ["## 週回報語意分析", fenced("json", json.dumps(out.analysis, ensure_ascii=False, indent=2))]
    if reply.parse_error:
        parts += ["## ⚠ 失敗", f"`{reply.parse_error}`"]
    return "\n\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="管到飛：用規則檔跑測試案例")
    parser.add_argument("cases", nargs="*", type=Path, help="案例檔路徑")
    parser.add_argument("--all", action="store_true", help=f"跑 {CASES_DIR.name}/ 下所有案例")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH, help="規則檔路徑")
    parser.add_argument("--provider", help="覆寫 LLM_PROVIDER（azure / openai）")
    parser.add_argument("--dry-run", action="store_true", help=f"不呼叫 LLM，把會送出的 messages 寫到 outputs/{DRY_RUN_DIRNAME}/")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cases = load_cases(CASES_DIR) if args.all else [load_case(p) for p in args.cases]
    if not cases:
        parser.error("請指定案例檔，或加 --all")
    rules = load_rules(args.rules)
    print(f"規則檔 {rules.path.name}  版本 {rules.version} ({rules.version_date})  sha {rules.sha256[:8]}")

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
    failed = 0
    for case in cases:
        reply = run_case(case, rules, llm)
        if case.expected_status and reply.status != case.expected_status:
            failed += 1
    print(f"\n{len(cases) - failed}/{len(cases)} 符合預期")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
