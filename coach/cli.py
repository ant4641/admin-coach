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
from coach.llm import LLMClient, LLMResult, get_llm
from coach.prompt import CoachReply, build_messages, parse_reply
from coach.rules import DEFAULT_RULES_PATH, Rules, load_rules

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


def render_dry_run(case: Case, rules: Rules) -> str:
    """會送給模型的內容，排成好讀的 Markdown。規則檔原文太長且固定，只註明來源。"""
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


def run_case(case: Case, rules: Rules, llm: LLMClient) -> CoachReply:
    result = llm.complete(build_messages(rules, case.body))
    reply = parse_reply(result.text)

    now = datetime.now().astimezone()
    generated_at = now.isoformat(timespec="seconds")
    out_dir = OUTPUTS_DIR / case.name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{now:%Y%m%d-%H%M%S}__rules-{rules.tag}__{llm.provider}.md"
    out_file.write_text(render_output(case, rules, llm, result, reply, generated_at), encoding="utf-8")

    match = case.expected_status is None or case.expected_status == reply.status
    append_csv(
        {
            "generated_at": generated_at,
            "case": case.name,
            "rules_version": rules.version,
            "rules_sha8": rules.sha256[:8],
            "provider": llm.provider,
            "model": result.model,
            "temperature": getattr(llm, "temperature", None),
            "expected_status": case.expected_status or "",
            "actual_status": reply.status,
            "match": match,
            "latency_ms": result.latency_ms,
            "prompt_tokens": result.usage.get("prompt_tokens", ""),
            "completion_tokens": result.usage.get("completion_tokens", ""),
            "output_file": out_file.relative_to(ROOT).as_posix(),
        }
    )
    flag = "OK  " if match else "FAIL"
    print(f"[{flag}] {case.name}: expected={case.expected_status} actual={reply.status} → {out_file.relative_to(ROOT)}")
    return reply


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
