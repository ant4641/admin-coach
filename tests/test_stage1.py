"""規則檔、輸入、prompt 組裝與解析、案例格式、表格顯示。全部不需金鑰。"""

import json
from pathlib import Path

import pytest

from coach import inputs as inp
from coach.cases import industry_number, load_case, load_cases, parse_answers
from coach.display import render_table, width
from coach.prompt import build_messages, inputs_message, parse_reply
from coach.rules import load_rules

ROOT = Path(__file__).resolve().parent.parent

ALL_FIELDS = dict(name="王主管", industry_no=9, product="P", sales="S", score=5, reason="R")


def make_inputs(**over):
    return inp.build_inputs(inp.parse_industries(load_rules().text), **{**ALL_FIELDS, **over})


# ---- 規則檔 ---------------------------------------------------------------


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
    assert '"status":"clarify"' in r.text and '"status":"vfp"' in r.text


# ---- 輸入 -----------------------------------------------------------------


def test_industries_come_from_rules_file():
    items = inp.parse_industries(load_rules().text)
    assert len(items) == 20
    assert items[0] == "餐飲業（餐廳、小吃、咖啡、飲料店）"
    assert inp.is_other(items[19])
    assert all(not inp.is_other(x) for x in items[:19])


def test_industries_numbering_must_be_continuous():
    with pytest.raises(ValueError):
        inp.parse_industries("### 2.1 x\n  1. a\n  3. b\n")
    with pytest.raises(ValueError):
        inp.parse_industries("沒有清單")


def test_question_texts_exist_in_rules_file():
    text = load_rules().text
    for q in (inp.Q_NAME, inp.Q_INDUSTRY, inp.Q_PRODUCT, inp.Q_SALES, inp.Q_SCORE, inp.Q_REASON):
        assert q in text


def test_build_inputs_stores_full_industry_text():
    i = make_inputs(industry_no=1)
    assert i["industry"] == "餐飲業（餐廳、小吃、咖啡、飲料店）" and i["industry_no"] == 1


def test_other_industry_needs_text_and_is_stored_with_prefix():
    with pytest.raises(ValueError):
        make_inputs(industry_no=20)
    assert make_inputs(industry_no=20, industry_other=" 修繕 ")["industry"] == "其他：修繕"


def test_only_name_and_industry_are_required():
    i = make_inputs(product="", sales="", score=None, reason="")
    assert (i["product"], i["sales"], i["score"], i["reason"]) == ("", "", None, "")
    with pytest.raises(ValueError):
        make_inputs(name="  ")


@pytest.mark.parametrize("bad", [0, 21, True, "9"])
def test_bad_industry_number(bad):
    with pytest.raises(ValueError):
        make_inputs(industry_no=bad)


@pytest.mark.parametrize("bad", [0, 11, True])
def test_bad_score(bad):
    with pytest.raises(ValueError):
        make_inputs(score=bad)


# ---- prompt ---------------------------------------------------------------


def test_messages_are_rules_then_inputs_only():
    r = load_rules()
    msgs = build_messages(r, make_inputs(), [])
    assert msgs == [{"role": "system", "content": r.text}, {"role": "user", "content": inputs_message(make_inputs())}]


def test_blank_fields_are_sent_as_unfilled_and_score_reason_are_not_sent():
    text = inputs_message(make_inputs(product="", sales="", reason="秘密原因", score=7))
    assert text.count(inp.UNFILLED) == 2
    assert "秘密原因" not in text and "7" not in text
    assert "王主管" in text and "設計、廣告、行銷與攝影" in text


def test_answered_exchanges_are_replayed_and_unanswered_one_is_not():
    ex = [
        {"coach": {"status": "clarify", "question": "Q1"}, "manager": "A1"},
        {"coach": {"status": "clarify", "question": "Q2"}, "manager": None},
    ]
    msgs = build_messages(load_rules(), make_inputs(), ex)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert json.loads(msgs[2]["content"]) == ex[0]["coach"] and msgs[3]["content"] == "A1"


# ---- 解析 -----------------------------------------------------------------

GOOD_VFP = {"status": "vfp", "vfp": "V", "byproducts": [{"task": "a", "detail": "b", "product": "p"}]}


def test_parse_clarify_and_vfp():
    c = parse_reply('{"status":"clarify","question":" Q? "}')
    assert (c.status, c.question) == ("clarify", "Q?")
    v = parse_reply("```json\n" + json.dumps(GOOD_VFP) + "\n```")
    assert (v.status, v.vfp, v.byproducts) == ("vfp", "V", [{"task": "a", "detail": "b", "product": "p"}])


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"status":"plan","message":"x"}',
        '{"status":"clarify"}',
        '{"status":"clarify","question":"  "}',
        '{"status":"vfp","vfp":"","byproducts":[{"task":"a","detail":"b","product":"p"}]}',
        '{"status":"vfp","vfp":"V"}',
        '{"status":"vfp","vfp":"V","byproducts":[]}',
        '{"status":"vfp","vfp":"V","byproducts":[{"name":"a"}]}',
        '{"status":"vfp","vfp":"V","byproducts":[{"task":"","detail":"b","product":"p"}]}',
        '{"status":"vfp","vfp":"V","byproducts":["a"]}',
    ],
)
def test_unusable_replies_are_unknown(raw):
    r = parse_reply(raw)
    assert r.status == "unknown" and r.parse_error


# ---- 案例 -----------------------------------------------------------------


def test_cases_have_expected_status():
    cases = {c.name: c for c in load_cases(ROOT / "test_cases")}
    assert len(cases) == 5
    assert all(c.expected_status in ("clarify", "vfp") for c in cases.values())
    assert not any(c.todo for c in cases.values())
    assert all(c.start_fields()["industry_no"] in range(1, 21) for c in cases.values())


def test_cases_without_answers_expect_clarify():
    """沒有 answers 的案例只跑第一輪，不能期望 vfp（不替主管編回答）。"""
    for c in load_cases(ROOT / "test_cases"):
        if not c.answers():
            assert c.expected_status == "clarify", c.name


@pytest.mark.parametrize("case", load_cases(ROOT / "test_cases"), ids=lambda c: c.name)
def test_case_industry_matches_rules_file_exactly(case):
    """## 產業 必須是「編號 + 規則檔 2.1 同編號的完整原文」，名稱不符就失敗。"""
    industries = inp.parse_industries(load_rules().text)
    no = industry_number(case.sections["產業"])
    assert case.sections["產業"] == f"{no} {industries[no - 1]}"


def test_blank_product_case_has_blank_product():
    assert load_case(ROOT / "test_cases" / "05_blank_product.md").start_fields()["product"] == ""


def test_case_parsing_with_answers(tmp_path):
    p = tmp_path / "c.md"
    p.write_text(
        "---\nexpected_status: vfp\n---\n## 稱呼\n甲\n\n## 產業\n20\n\n## 其他產業說明\n乙\n\n## 自評分數\n6\n\n## answers\n- 第一\n- 第二\n  續行\n",
        encoding="utf-8",
    )
    c = load_case(p)
    f = c.start_fields()
    assert (f["name"], f["industry_no"], f["industry_other"], f["score"]) == ("甲", 20, "乙", 6)
    assert c.answers() == ["第一", "第二\n續行"]


@pytest.mark.parametrize(
    "text",
    [
        "---\nexpected_status: plan\n---\n## 稱呼\n甲\n\n## 產業\n1",
        "---\nexpected_status: vfp\n---\n## 產業\n1",
        "---\nexpected_status: vfp\n---\n## 稱呼\n甲\n\n## 產業\n九",
        "---\nexpected_status: vfp\n---\n## 稱呼\n甲\n\n## 產業\n1\n\n## 自評分數\n高",
    ],
)
def test_bad_case_files(tmp_path, text):
    p = tmp_path / "c.md"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_case(p)


def test_industry_number_may_be_followed_by_text():
    assert industry_number("8") == 8
    assert industry_number("8 資訊、軟體與網路服務") == 8
    for bad in ("", "九", "資訊 8"):
        with pytest.raises(ValueError):
            industry_number(bad)


def test_parse_answers():
    assert parse_answers("- a\n- b") == ["a", "b"]
    assert parse_answers("") == []


# ---- 表格顯示 -------------------------------------------------------------


def test_table_lines_have_equal_display_width():
    t = render_table("吃到好吃且想再來的客人", [{"task": "購買食材", "detail": "麵條、豬骨、配料", "product": "p"}, {"task": "A", "detail": "x" * 120, "product": "p"}])
    widths = {width(line) for line in t.splitlines()}
    assert len(widths) == 1
    lines = t.splitlines()
    assert "VFP" in lines[1] and "吃到好吃且想再來的客人" in lines[1]
    assert "工作項目" in lines[3] and "副產品" in lines[3]
    assert lines[4].startswith("+=")
    # 副產品由上到下維持輸入順序
    assert t.index("購買食材") < t.index("| A ")


# ---- byproducts 三欄格式（task / detail / product）------------------------------


@pytest.mark.parametrize(
    "item",
    [
        {"detail": "b", "product": "p"},
        {"task": "a", "product": "p"},
        {"task": "a", "detail": "b"},
        {"task": "a", "detail": "b", "product": " "},
        {"task": "a", "detail": "", "product": "p"},
        {"task": "a", "detail": "b", "product": 3},
        {"name": "a", "description": "b"},
    ],
)
def test_vfp_reply_needs_three_nonempty_string_fields(item):
    raw = json.dumps({"status": "vfp", "vfp": "V", "byproducts": [{"task": "x", "detail": "y", "product": "z"}, item]}, ensure_ascii=False)
    assert parse_reply(raw).status == "unknown"


def test_vfp_reply_new_format_ok():
    raw = '{"status":"vfp","vfp":"V","byproducts":[{"task":" 備料 ","detail":"d","product":"p"}]}'
    assert parse_reply(raw).byproducts == [{"task": "備料", "detail": "d", "product": "p"}]
