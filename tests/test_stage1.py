import csv
from pathlib import Path

import pytest

from coach import cli
from coach.cases import load_case, load_cases
from coach.prompt import OUTPUT_CONTRACT, build_messages, parse_reply
from coach.rules import load_rules
from tests.fakes import ScriptedLLM, reply

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def tmp_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path.parent)
    return tmp_path


def test_rules_version_from_changelog(tmp_path):
    p = tmp_path / "r.md"
    p.write_text("# x\n| 版本 | 日期 |\n|---|---|\n| v0.1 | 2026-09-28 | a |\n| v0.2 | 2026-10-05 | b |\n", encoding="utf-8")
    r = load_rules(p)
    assert (r.version, r.version_date) == ("v0.2", "2026-10-05")
    assert r.tag == f"v0.2-{r.sha256[:8]}"


def test_rules_without_changelog(tmp_path):
    p = tmp_path / "r.md"
    p.write_text("no table", encoding="utf-8")
    assert load_rules(p).version == "unversioned"


def test_real_rules_file_loads():
    r = load_rules()
    assert r.version.startswith("v")
    assert "行政管理等級表" in r.text


def test_cases_have_expected_status():
    cases = load_cases(ROOT / "test_cases")
    assert len(cases) >= 3
    assert all(c.expected_status in ("plan", "clarify") for c in cases)


def test_bad_expected_status(tmp_path):
    p = tmp_path / "c.md"
    p.write_text("---\nexpected_status: maybe\n---\nhi", encoding="utf-8")
    with pytest.raises(ValueError):
        load_case(p)


def test_messages_keep_rules_verbatim():
    r = load_rules()
    msgs = build_messages(r, "hello")
    assert msgs[0] == {"role": "system", "content": r.text}
    assert msgs[1]["content"] == OUTPUT_CONTRACT
    assert msgs[2] == {"role": "user", "content": "hello"}


@pytest.mark.parametrize(
    "raw,status",
    [
        ('{"status":"plan","missing":[],"message":"x"}', "plan"),
        ('```json\n{"status":"clarify","missing":["a"],"message":"q"}\n```', "clarify"),
        ('{"status":"other","message":"x"}', "unknown"),
        ("not json", "unknown"),
    ],
)
def test_parse_reply(raw, status):
    assert parse_reply(raw).status == status


CASES = load_cases(ROOT / "test_cases")


def _use(monkeypatch, llm):
    monkeypatch.setattr(cli, "get_llm", lambda provider=None: llm)
    return llm


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_llm_receives_rules_and_case_verbatim(tmp_outputs, monkeypatch, case):
    llm = _use(monkeypatch, ScriptedLLM(reply("plan")))
    cli.main([str(case.path)])
    (messages,) = llm.calls
    assert messages[0] == {"role": "system", "content": load_rules().text}
    assert messages[-1] == {"role": "user", "content": case.body}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_output_file_and_csv_record_metadata(tmp_outputs, monkeypatch, case):
    _use(monkeypatch, ScriptedLLM(reply("clarify")))
    cli.main([str(case.path)])
    (out,) = (tmp_outputs / case.name).glob("*.md")
    rules = load_rules()
    assert f"rules-{rules.tag}" in out.name
    text = out.read_text(encoding="utf-8")
    for key in ("rules_version", "rules_sha256", "generated_at", "expected_status", "actual_status"):
        assert f"{key}:" in text
    with (tmp_outputs / "runs.csv").open(encoding="utf-8-sig") as f:
        (row,) = list(csv.DictReader(f))
    assert row["case"] == case.name and row["rules_sha8"] == rules.sha256[:8]
    assert row["match"] == str(case.expected_status == "clarify")


@pytest.mark.parametrize("status", ["plan", "clarify"])
def test_exit_code_reflects_expected_vs_actual(tmp_outputs, monkeypatch, status):
    _use(monkeypatch, ScriptedLLM(reply(status)))
    code = cli.main(["--all"])
    all_match = all(c.expected_status == status for c in CASES)
    assert code == (0 if all_match else 1)


def test_unparseable_reply_is_not_a_match(tmp_outputs, monkeypatch):
    _use(monkeypatch, ScriptedLLM("not json"))
    assert cli.main(["--all"]) == 1
    assert "解析失敗" in next(tmp_outputs.rglob("*.md")).read_text(encoding="utf-8")


def test_dry_run_never_calls_llm(tmp_outputs, monkeypatch, capsys):
    def boom(provider=None):
        raise AssertionError("dry-run 不應建立 LLM client")

    monkeypatch.setattr(cli, "get_llm", boom)
    assert cli.main(["--all", "--dry-run"]) == 0
    assert not (tmp_outputs / "runs.csv").exists()
