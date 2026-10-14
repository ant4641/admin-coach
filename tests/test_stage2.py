"""階段 2-lite：流程狀態轉換、存取層、API、命令列。

全部用 tests/fakes.py 的替身，不需金鑰；只驗證「流程怎麼轉」，不驗證規則對模型的效果。
"""

import csv
import json

import pytest
from fastapi.testclient import TestClient

from coach import chat, cli
from coach.api import create_app
from coach.cases import load_case, parse_sections
from coach.flow import (
    READY_MESSAGE,
    SHAKY_QUESTION,
    BadModelReply,
    CoachService,
    InvalidInput,
    LLMUnavailable,
    WrongState,
)
from coach.prompt import ADJUST_CONTRACT, OUTPUT_CONTRACT, WEEKLY_CONTRACT
from coach.rules import load_rules
from coach.store import JsonFileStore, SessionNotFound
from tests.fakes import SequenceLLM, clarify, plan

TABLE_V1 = "【等級表】\n1. 目標：甲\n2. 目的：乙（暫定）\n3. 規章：丙"
TABLE_V2 = "【等級表】\n1. 目標：甲\n2. 目的：丁（暫定）\n3. 規章：丙"
CHANGE = {"level": 2, "name": "目的", "type": "修改", "before": "乙", "after": "丁", "reason": "測試"}
WEEKLY = dict(actual="做了一半", score=60, achieved="A", not_achieved="B", reason="C")


@pytest.fixture
def make(tmp_path):
    def _make(*replies):
        llm = SequenceLLM(*replies)
        return CoachService(JsonFileStore(tmp_path / "sessions"), lambda: llm), llm

    return _make


def start(svc):
    return svc.start("林主管", "理想畫面", "現況")


def ready_session(make, *more_replies):
    """已進入執行（信心 80）的 session，之後的模型回覆由 more_replies 提供。"""
    svc, llm = make(plan(TABLE_V1), *more_replies)
    sid = start(svc).session_id
    svc.confidence(sid, 80)
    return svc, llm, sid


# ---- 存取層 --------------------------------------------------------------


def test_store_roundtrip(tmp_path):
    store = JsonFileStore(tmp_path / "sessions")
    sid = store.new_id()
    store.save({"id": sid, "名字": "林主管"})
    assert store.load(sid) == {"id": sid, "名字": "林主管"}
    assert (tmp_path / "sessions" / f"{sid}.json").exists()
    assert not list((tmp_path / "sessions").glob("*.tmp"))


@pytest.mark.parametrize("bad", ["nope", "../x", "a/b", "..", ""])
def test_store_rejects_unknown_or_unsafe_ids(tmp_path, bad):
    with pytest.raises(SessionNotFound):
        JsonFileStore(tmp_path).load(bad)


# ---- 建立與追問 ----------------------------------------------------------


def test_start_plan_saves_session_file(make, tmp_path):
    svc, llm = make(plan(TABLE_V1))
    out = start(svc)
    assert (out.status, out.version, out.message) == ("plan", 1, TABLE_V1)
    saved = json.loads((tmp_path / "sessions" / f"{out.session_id}.json").read_text(encoding="utf-8"))
    assert (saved["name"], saved["ideal"], saved["current"]) == ("林主管", "理想畫面", "現況")
    (v,) = saved["versions"]
    rules = load_rules()
    assert (v["rules_version"], v["rules_sha8"], v["trigger"]) == (rules.version, rules.sha256[:8], "initial")
    assert v["created_at"] and v["table"] == TABLE_V1


def test_start_sends_rules_verbatim_and_inputs(make):
    svc, llm = make(plan(TABLE_V1))
    start(svc)
    (msgs,) = llm.calls
    assert msgs[0] == {"role": "system", "content": load_rules().text}
    assert msgs[1]["content"] == OUTPUT_CONTRACT
    assert "林主管" in msgs[-1]["content"] and "理想畫面" in msgs[-1]["content"] and "現況" in msgs[-1]["content"]


def test_clarify_then_answer_then_plan(make):
    svc, llm = make(clarify("客人走進店裡先遇到誰？"), plan(TABLE_V1))
    out = start(svc)
    assert (out.status, out.kind, out.message) == ("clarify", "intake", "客人走進店裡先遇到誰？")
    out2 = svc.answer(out.session_id, "迎賓員")
    assert (out2.status, out2.version) == ("plan", 1)
    msgs = llm.calls[1]  # 第二次呼叫要帶著上一輪的一問一答
    assert [m["role"] for m in msgs[-3:]] == ["user", "assistant", "user"]
    assert "客人走進店裡先遇到誰？" in msgs[-2]["content"] and msgs[-1]["content"] == "迎賓員"
    assert svc.get(out.session_id)["intake"]["exchanges"][0]["manager"] == "迎賓員"


def test_intake_can_ask_again(make):
    svc, _ = make(clarify("q1"), clarify("q2"), plan(TABLE_V1))
    out = start(svc)
    assert svc.answer(out.session_id, "a1").message == "q2"
    assert svc.answer(out.session_id, "a2").status == "plan"


def test_unusable_model_reply_is_not_saved(make, tmp_path):
    svc, _ = make("not json")
    with pytest.raises(BadModelReply):
        start(svc)
    assert not (tmp_path / "sessions").exists() or not list((tmp_path / "sessions").glob("*.json"))


def test_llm_failure_is_reported_not_saved(tmp_path):
    def boom():
        raise RuntimeError("缺少環境變數")

    svc = CoachService(JsonFileStore(tmp_path), boom)
    with pytest.raises(LLMUnavailable):
        start(svc)


def test_failed_answer_keeps_session_retryable(make):
    svc, _ = make(clarify("q"), "not json", plan(TABLE_V1))
    sid = start(svc).session_id
    with pytest.raises(BadModelReply):
        svc.answer(sid, "a")
    s = svc.get(sid)
    assert s["status"] == "clarify" and s["intake"]["exchanges"][0]["manager"] is None
    assert svc.answer(sid, "a").status == "plan"  # 同一個回答可直接重送


# ---- 信心分數：79 / 80 邊界 ----------------------------------------------


def test_confidence_79_asks_the_one_question_without_calling_model(make):
    svc, llm = make(plan(TABLE_V1))
    sid = start(svc).session_id
    out = svc.confidence(sid, 79)
    assert (out.status, out.kind, out.message) == ("clarify", "shaky_item", SHAKY_QUESTION)
    assert SHAKY_QUESTION == "最讓你沒把握的是哪一項？"
    assert len(llm.calls) == 1  # 只有建立時那一次
    s = svc.get(sid)
    assert s["status"] == "clarify" and s["confidences"][0]["score"] == 79 and s["confidences"][0]["version"] == 1


@pytest.mark.parametrize("score", [80, 81, 100])
def test_confidence_80_or_more_is_ready_with_reminder(make, score):
    svc, llm = make(plan(TABLE_V1))
    sid = start(svc).session_id
    out = svc.confidence(sid, score)
    assert (out.status, out.message) == ("ready", READY_MESSAGE)
    assert "回報" in READY_MESSAGE
    assert svc.get(sid)["status"] == "ready" and len(llm.calls) == 1


@pytest.mark.parametrize("score", [0, 1, 78])
def test_low_scores_go_to_adjusting(make, score):
    svc, _ = make(plan(TABLE_V1))
    sid = start(svc).session_id
    assert svc.confidence(sid, score).kind == "shaky_item"


@pytest.mark.parametrize("score", [-1, 101, 79.5, "80", True])
def test_confidence_rejects_bad_scores(make, score):
    svc, _ = make(plan(TABLE_V1))
    sid = start(svc).session_id
    with pytest.raises(InvalidInput):
        svc.confidence(sid, score)
    assert svc.get(sid)["status"] == "plan"


# ---- 低於 80 的調整流程 ---------------------------------------------------


def low_confidence_session(make, *replies):
    svc, llm = make(plan(TABLE_V1), *replies)
    sid = start(svc).session_id
    svc.confidence(sid, 70)
    return svc, llm, sid


UPSTREAM = {"level": 2, "item": "乙", "concern": "太高", "suggestion": "改成丁"}


def test_adjust_without_upstream_problem(make):
    svc, llm, sid = low_confidence_session(make, plan(TABLE_V2, upstream_decision="none", changes=[CHANGE]))
    out = svc.answer(sid, "不知道怎麼排班")
    assert (out.status, out.version, out.upstream_decision) == ("plan", 2, "none")
    s = svc.get(sid)
    assert [v["no"] for v in s["versions"]] == [1, 2]
    assert s["versions"][1]["trigger"] == "adjust" and s["versions"][1]["based_on"] == 1
    assert s["confidences"][0]["result_version"] == 2
    msgs = llm.calls[1]
    assert msgs[2]["content"] == ADJUST_CONTRACT
    assert TABLE_V1 in msgs[3]["content"] and "70" in msgs[3]["content"]
    assert SHAKY_QUESTION in msgs[4]["content"] and msgs[5]["content"] == "不知道怎麼排班"


def test_upstream_is_proposed_then_changed_only_after_manager_agrees(make):
    svc, llm, sid = low_confidence_session(
        make,
        clarify("目的是不是訂太高？要調整嗎？", kind="upstream_confirm", upstream=UPSTREAM),
        plan(TABLE_V2, upstream_decision="accepted", changes=[CHANGE]),
    )
    out = svc.answer(sid, "預算很緊")
    assert (out.status, out.kind, out.upstream) == ("clarify", "upstream_confirm", UPSTREAM)
    assert len(svc.get(sid)["versions"]) == 1  # 提議階段不產出新版本，上游沒被動
    out = svc.answer(sid, "好，調整")
    assert (out.status, out.version, out.upstream_decision) == ("plan", 2, "accepted")
    assert out.changes == [CHANGE]
    assert "已經提議過" in llm.calls[2][3]["content"]  # 第二輪起，程式提示模型不可再提議
    s = svc.get(sid)
    assert len(s["confidences"][0]["exchanges"]) == 2
    # 調整後要重新給分
    assert svc.confidence(sid, 85).status == "ready"
    assert [c["score"] for c in svc.get(sid)["confidences"]] == [70, 85]


def test_manager_declines_upstream_change_gets_advice(make):
    svc, _, sid = low_confidence_session(
        make,
        clarify("要調整嗎？", kind="upstream_confirm", upstream=UPSTREAM),
        plan(TABLE_V2, upstream_decision="declined", advice="上下游不一致，等級表會受阻，建議……"),
    )
    svc.answer(sid, "預算緊")
    out = svc.answer(sid, "不要調整")
    assert (out.status, out.upstream_decision) == ("plan", "declined")
    assert "受阻" in out.advice
    assert svc.get(sid)["versions"][1]["advice"] == out.advice


def test_declined_without_advice_is_rejected(make):
    svc, _, sid = low_confidence_session(
        make,
        clarify("要調整嗎？", kind="upstream_confirm", upstream=UPSTREAM),
        plan(TABLE_V2, upstream_decision="declined"),
    )
    svc.answer(sid, "預算緊")
    with pytest.raises(BadModelReply):
        svc.answer(sid, "不要")
    assert len(svc.get(sid)["versions"]) == 1


def test_upstream_cannot_be_proposed_twice(make):
    svc, _, sid = low_confidence_session(
        make,
        clarify("要調整嗎？", kind="upstream_confirm", upstream=UPSTREAM),
        clarify("再問一次要調整嗎？", kind="upstream_confirm", upstream=UPSTREAM),
    )
    svc.answer(sid, "預算緊")
    with pytest.raises(BadModelReply):
        svc.answer(sid, "嗯")
    s = svc.get(sid)
    assert s["status"] == "clarify" and s["confidences"][0]["exchanges"][-1]["manager"] is None


def test_vague_answer_gets_info_question(make):
    svc, _, sid = low_confidence_session(make, clarify("是哪一個步驟？", kind="info"), plan(TABLE_V2, upstream_decision="none"))
    out = svc.answer(sid, "都沒把握")
    assert (out.status, out.kind) == ("clarify", "adjust_info")
    assert svc.answer(sid, "第三步").status == "plan"


# ---- 狀態防呆 ------------------------------------------------------------


def test_actions_in_wrong_state_are_refused(make):
    svc, _ = make(clarify("q"), plan(TABLE_V1))
    sid = start(svc).session_id  # clarify
    with pytest.raises(WrongState):
        svc.confidence(sid, 90)
    with pytest.raises(WrongState):
        svc.weekly_report(sid, **WEEKLY)
    svc.answer(sid, "a")  # plan
    with pytest.raises(WrongState):
        svc.answer(sid, "多餘的回答")
    with pytest.raises(WrongState):
        svc.weekly_report(sid, **WEEKLY)
    svc.confidence(sid, 90)  # ready
    with pytest.raises(WrongState):
        svc.confidence(sid, 90)
    with pytest.raises(WrongState):
        svc.answer(sid, "x")


def test_unknown_session(make):
    svc, _ = make()
    for call in (lambda: svc.get("nope"), lambda: svc.answer("nope", "x"), lambda: svc.confidence("nope", 90)):
        with pytest.raises(SessionNotFound):
            call()


# ---- 每週回報 ------------------------------------------------------------


def test_weekly_report_makes_new_version_and_marks_the_difference(make):
    analysis = {
        "positive": ["A 達成"],
        "negative": ["B 沒做到"],
        "neutral": [],
        "items": [{"item": "目的", "type": "做了但現場沒有更接近理想畫面", "evidence": "C", "action": "修正"}],
    }
    svc, llm, sid = ready_session(make, plan(TABLE_V2, changes=[CHANGE], analysis=analysis))
    out = svc.weekly_report(sid, **WEEKLY)

    assert (out.status, out.version) == ("plan", 2)  # §7.4：新表要重新給分，所以回到 plan
    assert out.changes == [CHANGE] and out.analysis == analysis
    # 差異由程式算：只標出變動那一行，沒改的行不會被標
    assert "-2. 目的：乙（暫定）" in out.diff and "+2. 目的：丁（暫定）" in out.diff
    assert "-1. 目標：甲" not in out.diff and "+1. 目標：甲" not in out.diff

    s = svc.get(sid)
    assert [v["table"] for v in s["versions"]] == [TABLE_V1, TABLE_V2]
    v2 = s["versions"][1]
    assert (v2["trigger"], v2["based_on"], v2["changes"]) == ("weekly", 1, [CHANGE]) and v2["diff_from_prev"] == out.diff
    (rep,) = s["weekly_reports"]
    assert (rep["actual"], rep["score"], rep["achieved"], rep["not_achieved"], rep["reason"]) == ("做了一半", 60, "A", "B", "C")
    assert rep["based_on_version"] == 1 and rep["result_version"] == 2 and rep["analysis"] == analysis

    msgs = llm.calls[1]
    assert msgs[2]["content"] == WEEKLY_CONTRACT
    assert TABLE_V1 in msgs[3]["content"] and "做了一半" in msgs[3]["content"] and "60" in msgs[3]["content"]


def test_weekly_cycle_goes_through_confidence_again_and_keeps_history(make):
    svc, llm, sid = ready_session(make, plan(TABLE_V2, changes=[CHANGE]), plan(TABLE_V2 + "\n4. 方針：戊"))
    svc.weekly_report(sid, **WEEKLY)
    with pytest.raises(WrongState):  # 新表還沒給分，不能直接再回報
        svc.weekly_report(sid, **WEEKLY)
    assert svc.confidence(sid, 90).status == "ready"
    out = svc.weekly_report(sid, actual="第二週", score=70)
    assert out.version == 3
    ctx = llm.calls[2][3]["content"]
    assert "更早之前的週回報" in ctx and "做了一半" in ctx and "第二週" in ctx and TABLE_V2 in ctx
    assert [r["no"] for r in svc.get(sid)["weekly_reports"]] == [1, 2]


def test_weekly_insufficient_info_asks_one_question_instead_of_a_table(make):
    svc, llm, sid = ready_session(make, clarify("哪一項有做？", kind="info"), plan(TABLE_V2, changes=[CHANGE]))
    out = svc.weekly_report(sid, actual="還好", score=50)
    assert (out.status, out.kind, out.message) == ("clarify", "weekly", "哪一項有做？")
    assert len(svc.get(sid)["versions"]) == 1
    out = svc.answer(sid, "客戶有回信")
    assert (out.status, out.version) == ("plan", 2)
    assert "客戶有回信" in llm.calls[2][-1]["content"]
    assert svc.get(sid)["weekly_reports"][0]["exchanges"][0]["manager"] == "客戶有回信"


def test_weekly_input_validation(make):
    svc, llm, sid = ready_session(make)
    for bad in (dict(actual="x", score=101), dict(actual="x", score=-1), dict(actual="  ", score=50)):
        with pytest.raises(InvalidInput):
            svc.weekly_report(sid, **bad)
    assert svc.get(sid)["weekly_reports"] == [] and len(llm.calls) == 1


def test_failed_weekly_report_is_not_recorded(make):
    svc, _, sid = ready_session(make, "not json", plan(TABLE_V2))
    with pytest.raises(BadModelReply):
        svc.weekly_report(sid, **WEEKLY)
    s = svc.get(sid)
    assert s["status"] == "ready" and s["weekly_reports"] == []
    assert svc.weekly_report(sid, **WEEKLY).version == 2


def test_seed_creates_ready_session_without_model(make):
    svc, llm = make()
    out = svc.seed("林主管", "理想", "現況", TABLE_V1, 85)
    assert out.status == "ready" and llm.calls == []
    s = svc.get(out.session_id)
    assert s["status"] == "ready" and s["versions"][0]["trigger"] == "provided"
    with pytest.raises(InvalidInput):
        svc.seed("a", "b", "c", TABLE_V1, 79)


# ---- API -----------------------------------------------------------------


@pytest.fixture
def client(make):
    def _client(*replies):
        svc, llm = make(*replies)
        return TestClient(create_app(svc)), llm

    return _client


def test_api_full_flow(client):
    c, _ = client(
        clarify("再具體一點？"),
        plan(TABLE_V1),
        clarify("要調整嗎？", kind="upstream_confirm", upstream=UPSTREAM),
        plan(TABLE_V2, upstream_decision="accepted", changes=[CHANGE]),
        plan(TABLE_V2 + "\n4. 方針：戊", changes=[CHANGE]),
    )
    body = {"name": "林主管", "ideal": "理想", "current": "現況"}
    r = c.post("/sessions", json=body)
    assert r.status_code == 200 and r.json()["status"] == "clarify"
    sid = r.json()["session_id"]

    r = c.post(f"/sessions/{sid}/answer", json={"answer": "迎賓員"})
    assert (r.json()["status"], r.json()["version"]) == ("plan", 1)

    r = c.post(f"/sessions/{sid}/confidence", json={"score": 79})
    assert (r.json()["status"], r.json()["message"]) == ("clarify", SHAKY_QUESTION)
    r = c.post(f"/sessions/{sid}/answer", json={"answer": "預算"})
    assert r.json()["kind"] == "upstream_confirm" and r.json()["upstream"] == UPSTREAM
    r = c.post(f"/sessions/{sid}/answer", json={"answer": "好"})
    assert (r.json()["status"], r.json()["version"]) == ("plan", 2)

    r = c.post(f"/sessions/{sid}/confidence", json={"score": 80})
    assert r.json()["status"] == "ready"

    r = c.post(f"/sessions/{sid}/weekly-report", json={"actual": "做了一半", "score": 60})
    assert r.status_code == 200 and r.json()["version"] == 3 and r.json()["diff"]

    full = c.get(f"/sessions/{sid}").json()
    assert full["status"] == "plan" and len(full["versions"]) == 3
    assert [x["score"] for x in full["confidences"]] == [79, 80]
    assert len(full["weekly_reports"]) == 1


def test_api_errors(client):
    c, _ = client(plan(TABLE_V1), "not json")
    assert c.get("/sessions/nope").status_code == 404
    assert c.post("/sessions/nope/confidence", json={"score": 90}).status_code == 404
    assert c.post("/sessions", json={"name": "林主管", "ideal": "", "current": "x"}).status_code == 422
    sid = c.post("/sessions", json={"name": "a", "ideal": "b", "current": "c"}).json()["session_id"]
    assert c.post(f"/sessions/{sid}/confidence", json={"score": 101}).status_code == 422
    assert c.post(f"/sessions/{sid}/confidence", json={"score": -1}).status_code == 422
    assert c.post(f"/sessions/{sid}/confidence", json={"score": 80, "note": "x"}).status_code == 200  # 只收分數，多的欄位被忽略
    assert c.post(f"/sessions/{sid}/answer", json={"answer": "x"}).status_code == 409
    assert c.post(f"/sessions/{sid}/weekly-report", json={"actual": "x", "score": 101}).status_code == 422
    r = c.post(f"/sessions/{sid}/weekly-report", json={"actual": "x", "score": 50})
    assert r.status_code == 502 and "無法解析" in r.json()["detail"]


def test_api_exposes_docs(client):
    c, _ = client()
    assert c.get("/docs").status_code == 200
    paths = c.get("/openapi.json").json()["paths"]
    assert set(paths) == {
        "/sessions",
        "/sessions/{session_id}/answer",
        "/sessions/{session_id}/confidence",
        "/sessions/{session_id}/weekly-report",
        "/sessions/{session_id}",
    }


# ---- 命令列互動 ----------------------------------------------------------


def scripted_input(*lines):
    it = iter(lines)

    def _input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    return _input


def test_chat_runs_the_whole_flow(make, capsys):
    svc, _ = make(
        clarify("再具體一點？"),
        plan(TABLE_V1),
        plan(TABLE_V2, upstream_decision="none", changes=[CHANGE]),
        plan(TABLE_V2 + "\n4. 方針：戊", changes=[CHANGE]),
    )
    inputs = scripted_input(
        "林主管", "理想", "現況",  # 建立
        "迎賓員",  # 回答追問 → 第 1 版
        "70", "不知道怎麼排",  # 低分 → 回答 → 第 2 版
        "85",  # 進入執行
        "y", "做了一半", "60", "A", "B", "C",  # 週回報 → 第 3 版
        "90",  # 重新給分
        "n",  # 不再回報，離開
    )
    assert chat.main([], input_fn=inputs, service=svc) == 0
    out = capsys.readouterr().out
    assert "再具體一點？" in out and SHAKY_QUESTION in out and "進入執行" in out and "第 3 版" in out and "與上一版相比改了哪裡" in out
    (sid,) = [p.stem for p in svc.store.directory.glob("*.json")]
    s = svc.get(sid)
    assert s["status"] == "ready" and len(s["versions"]) == 3 and len(s["weekly_reports"]) == 1


def test_chat_resume_and_weekly_file(make, tmp_path, capsys):
    svc, _, sid = ready_session(make, plan(TABLE_V2, changes=[CHANGE]))
    f = tmp_path / "w.md"
    f.write_text("## 實際狀況\n做了一半\n\n## 本週成果分數\n60\n\n## 哪些沒達成\nB\n", encoding="utf-8")
    assert chat.main(["--resume", sid, "--weekly", str(f)], input_fn=scripted_input("90", "n"), service=svc) == 0
    s = svc.get(sid)
    assert s["weekly_reports"][0]["not_achieved"] == "B" and s["status"] == "ready"


def test_chat_reports_errors_and_missing_session(make, capsys):
    svc, _ = make("not json")
    assert chat.main([], input_fn=scripted_input("a", "b", "c"), service=svc) == 1
    assert "BadModelReply" in capsys.readouterr().out
    assert chat.main(["--resume", "nope"], input_fn=scripted_input(), service=svc) == 1


# ---- 週回報測試案例 ------------------------------------------------------

WEEKLY_CASE = """\
---
title: 測試用週回報案例（內容僅供測試程式，非真實案例）
kind: weekly
confidence: 85
expected_status: plan
---
## 稱呼
測試主管

## 理想情境
測試理想

## 現在情境
測試現況

## 上週等級表
{table}

## 實際狀況
測試實際狀況

## 本週成果分數
60

## 哪些沒達成
測試項目
"""


@pytest.fixture
def case_env(tmp_path, monkeypatch):
    out, cases = tmp_path / "outputs", tmp_path / "cases"
    out.mkdir(), cases.mkdir()
    monkeypatch.setattr(cli, "OUTPUTS_DIR", out)
    monkeypatch.setattr(cli, "CASES_DIR", cases)
    monkeypatch.setattr(cli, "ROOT", tmp_path)

    def use(llm):
        monkeypatch.setattr(cli, "get_llm", lambda provider=None: llm)
        return llm

    return out, cases, use


def test_parse_sections():
    s = parse_sections("---\nfront: x\n---\n## 甲\n一\n二\n\n## 乙\n三")
    assert s == {"甲": "一\n二", "乙": "三"}


def test_weekly_case_validation(tmp_path):
    def write(text):
        p = tmp_path / "c.md"
        p.write_text(text, encoding="utf-8")
        return p

    ok = write(WEEKLY_CASE.format(table=TABLE_V1))
    assert load_case(ok).kind == "weekly" and load_case(ok).weekly_fields()["score"] == 60
    with pytest.raises(ValueError, match="缺少段落"):
        write(WEEKLY_CASE.format(table=TABLE_V1).replace("## 實際狀況\n測試實際狀況\n\n", ""))
        load_case(tmp_path / "c.md")
    with pytest.raises(ValueError, match="confidence"):
        load_case(write(WEEKLY_CASE.format(table=TABLE_V1).replace("confidence: 85", "confidence: 79")))
    with pytest.raises(ValueError, match="base_case"):
        load_case(write("---\nkind: weekly\nconfidence: 80\n---\n## 實際狀況\nx\n\n## 本週成果分數\n50"))


def test_weekly_case_with_provided_table(case_env):
    out, cases, use = case_env
    (cases / "w.md").write_text(WEEKLY_CASE.format(table=TABLE_V1), encoding="utf-8")
    llm = use(SequenceLLM(plan(TABLE_V2, changes=[CHANGE])))
    assert cli.main(["--all"]) == 0
    (md,) = (out / "w").glob("*.md")
    text = md.read_text(encoding="utf-8")
    assert "actual_status: \"plan\"" in text and "與上週相比改了哪裡" in text and "+2. 目的：丁（暫定）" in text
    with (out / "runs.csv").open(encoding="utf-8-sig") as f:
        (row,) = list(csv.DictReader(f))
    assert (row["case"], row["actual_status"], row["match"]) == ("w", "plan", "True")
    (sess,) = (out / "sessions").glob("*.json")
    assert json.loads(sess.read_text(encoding="utf-8"))["versions"][0]["trigger"] == "provided"
    assert len(llm.calls) == 1  # 提供了等級表，所以只有週回報那一次呼叫模型


def test_weekly_case_mismatch_is_exit_1(case_env):
    out, cases, use = case_env
    (cases / "w.md").write_text(WEEKLY_CASE.format(table=TABLE_V1), encoding="utf-8")
    use(SequenceLLM(clarify("哪一項？")))
    assert cli.main(["--all"]) == 1


def test_weekly_case_with_base_case(case_env):
    out, cases, use = case_env
    (cases / "base.md").write_text(
        "---\nexpected_status: plan\n---\n## 稱呼\nA\n\n## 理想情境\nB\n\n## 現在情境\nC", encoding="utf-8"
    )
    (cases / "w.md").write_text(
        "---\nkind: weekly\nbase_case: base\nconfidence: 80\nexpected_status: plan\n---\n## 實際狀況\nx\n\n## 本週成果分數\n50",
        encoding="utf-8",
    )
    use(SequenceLLM(plan(TABLE_V1), plan(TABLE_V1), plan(TABLE_V2, changes=[CHANGE])))  # base、w 各自重新產第一版
    assert cli.main(["--all"]) == 0


def test_weekly_case_base_not_plan_never_counts_as_match(case_env):
    out, cases, use = case_env
    (cases / "base.md").write_text("---\nexpected_status: clarify\n---\n## 稱呼\nA\n\n## 理想情境\nB\n\n## 現在情境\nC", encoding="utf-8")
    (cases / "w.md").write_text(
        "---\nkind: weekly\nbase_case: base\nconfidence: 80\nexpected_status: clarify\n---\n## 實際狀況\nx\n\n## 本週成果分數\n50",
        encoding="utf-8",
    )
    use(SequenceLLM(clarify("q"), clarify("q")))
    assert cli.main(["--all"]) == 1  # base 回 clarify → w 記成 base_clarify，即使預期 clarify 也不算符合


def test_weekly_case_dry_run(case_env):
    out, cases, use = case_env
    (cases / "w.md").write_text(WEEKLY_CASE.format(table=TABLE_V1), encoding="utf-8")
    assert cli.main(["--all", "--dry-run"]) == 0
    text = (out / "_dry_run" / "w.md").read_text(encoding="utf-8")
    assert "測試實際狀況" in text and TABLE_V1 in text
