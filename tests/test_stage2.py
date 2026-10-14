"""存取層、流程狀態轉換、命令列（互動與案例執行器）。

全部用 tests/fakes.py 的替身，不需金鑰；只驗證「流程怎麼轉」，不驗證規則對模型的效果。
"""

import csv
import json
from pathlib import Path

import pytest

from coach import chat, cli
from coach import inputs as inp
from coach.cases import industry_number, load_case
from coach.flow import BadModelReply, CoachService, InvalidInput, LLMUnavailable, WrongState
from coach.rules import load_rules
from coach.store import JsonFileStore, SessionNotFound
from tests.fakes import ScriptedLLM, SequenceLLM, clarify, vfp

ROOT = Path(__file__).resolve().parent.parent
START = dict(name="王主管", industry_no=9, product="我們做設計", sales="普通", score=5, reason="普通")


@pytest.fixture
def make(tmp_path):
    def _make(*replies):
        llm = SequenceLLM(*replies)
        return CoachService(JsonFileStore(tmp_path / "sessions"), lambda: llm), llm

    return _make


# ---- 存取層 ---------------------------------------------------------------


def test_store_roundtrip(tmp_path):
    store = JsonFileStore(tmp_path / "sessions")
    sid = store.new_id()
    store.save({"id": sid, "x": "中文"})
    assert store.load(sid) == {"id": sid, "x": "中文"}
    with pytest.raises(SessionNotFound):
        store.load("nope")


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "a" * 65, "a.b"])
def test_store_rejects_unknown_or_unsafe_ids(tmp_path, bad):
    with pytest.raises(SessionNotFound):
        JsonFileStore(tmp_path).load(bad)


# ---- 流程 -----------------------------------------------------------------


def test_start_clarify_saves_session_with_all_six_inputs(make, tmp_path):
    svc, _ = make(clarify("Q1"))
    out = svc.start(**START)
    assert (out.status, out.question) == ("clarify", "Q1")
    s = svc.get(out.session_id)
    assert s["status"] == "clarify" and s["pending"] == {"question": "Q1"}
    assert s["inputs"]["industry"] == "設計、廣告、行銷與攝影"
    assert (s["inputs"]["score"], s["inputs"]["reason"]) == (5, "普通")
    assert (tmp_path / "sessions" / f"{out.session_id}.json").exists()


def test_first_round_can_go_straight_to_vfp(make):
    svc, _ = make(vfp("V"))
    out = svc.start(**START)
    assert (out.status, out.vfp) == ("vfp", "V") and out.byproducts and not out.edited
    s = svc.get(out.session_id)
    assert s["pending"] is None and s["result"]["original"]["vfp"] == "V" and s["result"]["edited"] is None
    r = load_rules()
    assert (s["result"]["rules_version"], s["result"]["rules_sha8"]) == (r.version, r.sha256[:8])


def test_clarify_answer_clarify_answer_vfp_and_model_sees_history(make):
    svc, llm = make(clarify("Q1"), clarify("Q2"), vfp("V"))
    sid = svc.start(**START).session_id
    assert svc.answer(sid, " A1 ").question == "Q2"
    assert svc.answer(sid, "A2").status == "vfp"
    final = llm.calls[-1]
    assert [m["role"] for m in final] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert final[0]["content"] == load_rules().text
    assert final[3]["content"] == "A1" and final[5]["content"] == "A2"
    s = svc.get(sid)
    assert [e["manager"] for e in s["exchanges"]] == ["A1", "A2"]


def test_score_and_reason_never_reach_the_model(make):
    svc, llm = make(clarify())
    svc.start(**{**START, "reason": "只存檔的原因", "score": 9})
    sent = json.dumps(llm.calls[0], ensure_ascii=False)
    assert "只存檔的原因" not in sent and "我們做設計" in sent and "普通" in sent


def test_blank_fields_go_to_model_as_unfilled(make):
    svc, llm = make(clarify())
    svc.start(name="王主管", industry_no=3)
    assert llm.calls[0][1]["content"].count("（未填）") == 2


def test_answer_after_vfp_is_409_and_calls_no_model(make):
    svc, llm = make(vfp())
    sid = svc.start(**START).session_id
    with pytest.raises(WrongState):
        svc.answer(sid, "more")
    assert len(llm.calls) == 1


def test_input_validation_errors_are_422_and_do_not_call_model(make):
    svc, llm = make(clarify())
    for bad in ({"name": " "}, {"industry_no": 21}, {"industry_no": 20}, {"score": 11}):
        with pytest.raises(InvalidInput):
            svc.start(**{**START, **bad})
    with pytest.raises(InvalidInput):
        svc.answer(svc.start(**START).session_id, "  ")
    assert len(llm.calls) == 1


def test_unusable_model_reply_is_not_saved_and_can_retry(make, tmp_path):
    svc, _ = make("not json", clarify("Q1"))
    with pytest.raises(BadModelReply):
        svc.start(**START)
    assert not list((tmp_path / "sessions").glob("*.json")) if (tmp_path / "sessions").exists() else True
    assert svc.start(**START).status == "clarify"


def test_failed_answer_keeps_session_retryable(make):
    svc, _ = make(clarify("Q1"), '{"status":"vfp","vfp":"V"}', vfp("V"))
    sid = svc.start(**START).session_id
    before = svc.get(sid)
    with pytest.raises(BadModelReply):
        svc.answer(sid, "A1")
    assert svc.get(sid) == before  # 沒存，回答也沒留下
    assert svc.answer(sid, "A1").status == "vfp"


def test_llm_failure_is_503_and_not_saved(tmp_path):
    def boom():
        raise RuntimeError("缺少環境變數")

    svc = CoachService(JsonFileStore(tmp_path / "sessions"), boom)
    with pytest.raises(LLMUnavailable):
        svc.start(**START)
    assert not (tmp_path / "sessions").exists()


def test_edit_result_keeps_both_versions(make):
    svc, _ = make(vfp("原始", [{"name": "a", "description": "b"}]))
    sid = svc.start(**START).session_id
    out = svc.edit_result(sid, " 改過 ", [{"name": "x", "description": "y"}, {"name": "z", "description": "w"}])
    assert out.edited and out.vfp == "改過" and [b["name"] for b in out.byproducts] == ["x", "z"]
    r = svc.get(sid)["result"]
    assert r["original"] == {"vfp": "原始", "byproducts": [{"name": "a", "description": "b"}]}
    assert r["edited"]["vfp"] == "改過" and len(r["edited"]["byproducts"]) == 2
    # 再改一次，原始版本仍在
    svc.edit_result(sid, "又改", [{"name": "q", "description": "r"}])
    assert svc.get(sid)["result"]["original"]["vfp"] == "原始"


def test_edit_result_only_in_vfp_state_and_validates(make):
    svc, _ = make(clarify("Q1"), vfp())
    sid = svc.start(**START).session_id
    with pytest.raises(WrongState):
        svc.edit_result(sid, "V", [{"name": "a", "description": "b"}])
    svc.answer(sid, "A")
    for vf, items in (("", [{"name": "a", "description": "b"}]), ("V", []), ("V", [{"name": "a", "description": ""}])):
        with pytest.raises(InvalidInput):
            svc.edit_result(sid, vf, items)


# ---- 命令列互動 -----------------------------------------------------------


def scripted_input(*lines):
    it = iter(lines)
    return lambda prompt: next(it)


def test_chat_runs_the_whole_flow_and_prints_table(make, capsys):
    svc, _ = make(clarify("你希望客人走出去變成什麼？"), vfp("開心的客人", [{"name": "備料", "description": "先備好"}]))
    # 稱呼 / 產業 / 產品 / 賣得如何 / 自評 / 原因 / 回答
    inputs = scripted_input("王主管", "9", "我們做設計", "", "", "", "開心")
    assert chat.main([], input_fn=inputs, service=svc) == 0
    out = capsys.readouterr().out
    assert "你希望客人走出去變成什麼？" in out and "開心的客人" in out and "| 備料" in out
    assert "9. 設計、廣告、行銷與攝影" in out  # 產業清單列給使用者看


def test_chat_other_industry_asks_for_text(make):
    svc, _ = make(vfp())
    inputs = scripted_input("王主管", "20", "修繕", "", "", "", "")
    chat.main([], input_fn=inputs, service=svc)
    (sid,) = [p.stem for p in svc.store.directory.glob("*.json")]
    assert svc.get(sid)["inputs"]["industry"] == "其他：修繕"


def test_chat_reprompts_bad_number(make, capsys):
    svc, _ = make(vfp())
    chat.main([], input_fn=scripted_input("王主管", "99", "abc", "1", "", "", "", ""), service=svc)
    assert capsys.readouterr().out.count("請輸入 1～20 的整數") == 2


def test_chat_resume_and_errors(make, capsys):
    svc, _ = make(clarify("Q1"), "not json")
    sid = svc.start(**START).session_id
    assert chat.main(["--resume", sid], input_fn=scripted_input("A1"), service=svc) == 1
    out = capsys.readouterr().out
    assert "Q1" in out and "沒有被存檔" in out
    assert chat.main(["--resume", "nope"], service=svc) == 1


# ---- 案例執行器 -----------------------------------------------------------


@pytest.fixture
def tmp_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path.parent)
    return tmp_path


@pytest.fixture
def case_dir(tmp_path, monkeypatch):
    d = tmp_path / "cases"
    d.mkdir()
    monkeypatch.setattr(cli, "CASES_DIR", d)
    return d


def write_case(d, name, status="clarify", answers=(), todo=False):
    body = "---\ntitle: T\nexpected_status: %s\n%s---\n" % (status, "todo: 待補\n" if todo else "")
    body += "## 稱呼\n甲\n\n## 產業\n9\n\n## 產品\n我們做設計\n"
    if answers:
        body += "\n## answers\n" + "\n".join(f"- {a}" for a in answers) + "\n"
    (d / f"{name}.md").write_text(body, encoding="utf-8")
    return d / f"{name}.md"


def use(monkeypatch, llm):
    monkeypatch.setattr(cli, "get_llm", lambda provider=None: llm)
    return llm


def test_case_without_answers_runs_only_first_round(tmp_outputs, case_dir, monkeypatch):
    write_case(case_dir, "a")
    llm = use(monkeypatch, ScriptedLLM(clarify("Q")))
    assert cli.main(["--all"]) == 0
    assert len(llm.calls) == 1


def test_case_with_answers_runs_multiple_rounds_until_vfp(tmp_outputs, case_dir, monkeypatch):
    write_case(case_dir, "a", status="vfp", answers=["A1", "A2", "A3"])
    llm = use(monkeypatch, SequenceLLM(clarify("Q1"), clarify("Q2"), vfp("V")))
    assert cli.main(["--all"]) == 0
    assert len(llm.calls) == 3  # 第 3 個回答用不到
    (out,) = (tmp_outputs / "a").glob("*.md")
    text = out.read_text(encoding="utf-8")
    assert "教練：Q1" in text and "主管：A1" in text and "| 備料" in text
    assert 'answers_used: "2/3"' in text and "rounds: 3" in text
    with (tmp_outputs / "runs.csv").open(encoding="utf-8-sig") as f:
        (row,) = list(csv.DictReader(f))
    rules = load_rules()
    assert (row["case"], row["match"], row["rounds"], row["byproducts"]) == ("a", "True", "3", "2")
    assert (row["rules_version"], row["rules_sha8"], row["actual_status"]) == (rules.version, rules.sha256[:8], "vfp")
    assert f"rules-{rules.tag}" in out.name


def test_answers_run_out_while_still_clarify_is_a_mismatch(tmp_outputs, case_dir, monkeypatch):
    write_case(case_dir, "a", status="vfp", answers=["A1"])
    use(monkeypatch, ScriptedLLM(clarify("Q")))
    assert cli.main(["--all"]) == 1


def test_mismatch_gives_exit_1(tmp_outputs, case_dir, monkeypatch):
    write_case(case_dir, "a", status="clarify")
    use(monkeypatch, ScriptedLLM(vfp()))
    assert cli.main(["--all"]) == 1


def test_safety_limit_of_10_rounds(tmp_outputs, case_dir, monkeypatch):
    write_case(case_dir, "a", status="vfp", answers=[f"A{i}" for i in range(30)])
    llm = use(monkeypatch, ScriptedLLM(clarify("Q")))
    assert cli.main(["--all"]) == 1
    assert len(llm.calls) == cli.MAX_ROUNDS == 10
    assert "over_limit" in next((tmp_outputs / "a").glob("*.md")).read_text(encoding="utf-8")


def test_unparseable_reply_is_a_failure_with_reason(tmp_outputs, case_dir, monkeypatch):
    write_case(case_dir, "a", status="clarify")
    use(monkeypatch, ScriptedLLM("not json"))
    assert cli.main(["--all"]) == 1
    assert "BadModelReply" in next((tmp_outputs / "a").glob("*.md")).read_text(encoding="utf-8")


def test_todo_case_is_skipped_not_run_and_not_failed(tmp_outputs, case_dir, monkeypatch, capsys):
    write_case(case_dir, "a", status="clarify", todo=True)
    llm = use(monkeypatch, ScriptedLLM(clarify("Q")))
    assert cli.main(["--all"]) == 0
    assert llm.calls == [] and not (tmp_outputs / "runs.csv").exists()
    assert "[SKIP] a" in capsys.readouterr().out


CASE_FILES = sorted((ROOT / "test_cases").glob("*.md"))


@pytest.mark.parametrize("path", CASE_FILES, ids=lambda p: p.stem)
def test_first_round_sent_to_model_matches_case_file(tmp_outputs, monkeypatch, path):
    """每個真實案例檔各跑一次：第一輪送給模型的內容必須與案例檔、規則檔一致。"""
    llm = use(monkeypatch, ScriptedLLM(clarify("Q")))
    cli.main([str(path)])
    first = llm.calls[0]
    case = load_case(path)
    sec = case.sections
    industries = inp.parse_industries(load_rules().text)
    no = industry_number(sec["產業"])

    assert len(first) == 2
    assert first[0] == {"role": "system", "content": load_rules().text}
    user = first[1]["content"]
    assert f"## {inp.Q_NAME}" + chr(10) + sec["稱呼"] + chr(10) in user
    assert f"## {inp.Q_INDUSTRY}" + chr(10) + industries[no - 1] + chr(10) in user
    assert f"## {inp.Q_PRODUCT}" + chr(10) + (sec.get("產品") or inp.UNFILLED) + chr(10) in user
    assert user.endswith(f"## {inp.Q_SALES}" + chr(10) + (sec.get("賣得如何") or inp.UNFILLED))
    # 自評分數與原因只存檔，不送模型：訊息只有四個段落，沒有這兩題
    assert user.count("## ") == 4
    assert inp.Q_SCORE not in user and inp.Q_REASON not in user
    # 案例有 answers 就會多輪；永遠回 clarify 的替身會把 answers 用完
    assert len(llm.calls) == 1 + len(case.answers())


def test_dry_run_never_calls_llm(tmp_outputs, monkeypatch):
    def boom(provider=None):
        raise AssertionError("dry-run 不應建立 LLM client")

    monkeypatch.setattr(cli, "get_llm", boom)
    assert cli.main(["--all", "--dry-run"]) == 0
    assert not (tmp_outputs / "runs.csv").exists()
    assert len(list((tmp_outputs / "_dry_run").glob("*.md"))) == 5
