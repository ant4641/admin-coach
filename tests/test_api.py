"""API：用 tests/fakes.py 的替身，不需金鑰。只驗證流程與錯誤碼，不驗證規則效果。"""

import pytest
from fastapi.testclient import TestClient

from coach.api import create_app
from coach.flow import CoachService
from coach.inputs import parse_industries
from coach.rules import load_rules
from coach.store import JsonFileStore
from tests.fakes import SequenceLLM, clarify, vfp

BODY = {"name": "王主管", "industry": 9}
ITEMS = [{"name": "a", "description": "b"}, {"name": "c", "description": "d"}]


@pytest.fixture
def make(tmp_path):
    def _make(*replies):
        llm = SequenceLLM(*replies)
        svc = CoachService(JsonFileStore(tmp_path / "sessions"), lambda: llm)
        return TestClient(create_app(svc)), llm

    return _make


def test_industries_come_from_rules_file(make):
    client, _ = make()
    r = client.get("/industries")
    assert r.status_code == 200
    items = parse_industries(load_rules().text)
    assert r.json() == [{"no": i, "name": n} for i, n in enumerate(items, start=1)]
    assert len(r.json()) == 20


def test_only_name_and_industry_are_required(make):
    client, llm = make(clarify("Q"))
    r = client.post("/sessions", json=BODY)
    assert r.status_code == 200 and r.json()["status"] == "clarify"
    assert llm.calls[0][1]["content"].count("（未填）") == 2


@pytest.mark.parametrize(
    "body,msg",
    [
        ({"industry": 9}, "name缺少"),
        ({"name": "王主管"}, "industry缺少"),
        ({"name": "王主管", "industry": "九"}, "industry格式不正確"),
        ({"name": "  ", "industry": 9}, "稱呼不可空白"),
        ({"name": "王主管", "industry": 0}, "產業必須是 1～20 的編號"),
        ({"name": "王主管", "industry": 21}, "產業必須是 1～20 的編號"),
        ({"name": "王主管", "industry": 9, "score": 11}, "營運自評分數必須是 1～10 的整數"),
    ],
)
def test_start_validation_returns_422_in_chinese_without_calling_model(make, body, msg):
    client, llm = make(clarify("Q"))
    r = client.post("/sessions", json=body)
    assert r.status_code == 422 and r.json() == {"detail": msg}
    assert llm.calls == []


def test_industry_20_requires_text_and_is_stored_with_prefix(make):
    client, llm = make(clarify("Q"))
    r = client.post("/sessions", json={"name": "王主管", "industry": 20})
    assert r.status_code == 422 and r.json() == {"detail": "選「其他」時必須填寫產業"}
    r = client.post("/sessions", json={"name": "王主管", "industry": 20, "industry_other": " 修繕 "})
    assert r.status_code == 200
    assert client.get(f"/sessions/{r.json()['session_id']}").json()["inputs"]["industry"] == "其他：修繕"
    assert "其他：修繕" in llm.calls[0][1]["content"]


def test_clarify_answer_vfp_flow(make):
    client, llm = make(clarify("Q1"), clarify("Q2"), vfp("V", ITEMS))
    sid = client.post("/sessions", json={**BODY, "product": "P", "score": 6, "reason": "R"}).json()["session_id"]
    r = client.post(f"/sessions/{sid}/answer", json={"answer": "A1"})
    assert r.json()["status"] == "clarify" and r.json()["question"] == "Q2"
    r = client.post(f"/sessions/{sid}/answer", json={"answer": "A2"})
    body = r.json()
    assert (body["status"], body["vfp"], body["byproducts"], body["edited"]) == ("vfp", "V", ITEMS, False)
    assert body["rules"]["version"] == load_rules().version
    s = client.get(f"/sessions/{sid}").json()
    assert s["status"] == "vfp" and s["inputs"]["score"] == 6 and s["inputs"]["reason"] == "R"
    assert [e["manager"] for e in s["exchanges"]] == ["A1", "A2"]
    assert len(llm.calls) == 3


def test_first_round_can_be_vfp(make):
    client, _ = make(vfp("V", ITEMS))
    assert client.post("/sessions", json=BODY).json()["status"] == "vfp"


def test_answer_validation_and_unknown_session(make):
    client, _ = make(clarify("Q"))
    sid = client.post("/sessions", json=BODY).json()["session_id"]
    assert client.post(f"/sessions/{sid}/answer", json={}).json() == {"detail": "answer缺少"}
    assert client.post(f"/sessions/{sid}/answer", json={"answer": "  "}).json() == {"detail": "回答不可為空"}
    for call in (
        lambda: client.get("/sessions/nope"),
        lambda: client.post("/sessions/nope/answer", json={"answer": "x"}),
        lambda: client.put("/sessions/nope/byproducts", json={"vfp": "V", "byproducts": ITEMS}),
    ):
        r = call()
        assert r.status_code == 404 and "找不到" in r.json()["detail"]


def test_answer_after_vfp_is_409(make):
    client, llm = make(vfp("V", ITEMS))
    sid = client.post("/sessions", json=BODY).json()["session_id"]
    r = client.post(f"/sessions/{sid}/answer", json={"answer": "more"})
    assert r.status_code == 409 and "沒有待回答的問題" in r.json()["detail"]
    assert len(llm.calls) == 1


def test_put_byproducts_is_409_unless_vfp(make):
    client, _ = make(clarify("Q"))
    sid = client.post("/sessions", json=BODY).json()["session_id"]
    r = client.put(f"/sessions/{sid}/byproducts", json={"vfp": "V", "byproducts": ITEMS})
    assert r.status_code == 409 and "只有 vfp 狀態能修改" in r.json()["detail"]


def test_put_overwrites_and_get_shows_original_and_edited(make):
    client, _ = make(vfp("原始", ITEMS))
    sid = client.post("/sessions", json=BODY).json()["session_id"]
    new = [{"name": "x", "description": "y"}]
    r = client.put(f"/sessions/{sid}/byproducts", json={"vfp": "改過", "byproducts": new})
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["vfp"], r.json()["byproducts"], r.json()["edited"]) == ("vfp", "改過", new, True)
    result = client.get(f"/sessions/{sid}").json()["result"]
    assert result["original"] == {"vfp": "原始", "byproducts": ITEMS}
    assert result["edited"]["vfp"] == "改過" and result["edited"]["byproducts"] == new
    # 再改一次，原始版本仍在；之後 answer 仍是 409
    client.put(f"/sessions/{sid}/byproducts", json={"vfp": "又改", "byproducts": ITEMS})
    result = client.get(f"/sessions/{sid}").json()["result"]
    assert result["original"]["vfp"] == "原始" and result["edited"]["vfp"] == "又改"
    assert client.post(f"/sessions/{sid}/answer", json={"answer": "x"}).status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"vfp": "", "byproducts": ITEMS},
        {"vfp": "V", "byproducts": []},
        {"vfp": "V", "byproducts": [{"name": "a", "description": ""}]},
        {"vfp": "V", "byproducts": [{"name": " ", "description": "b"}]},
    ],
)
def test_put_validation(make, body):
    client, _ = make(vfp("V", ITEMS))
    sid = client.post("/sessions", json=BODY).json()["session_id"]
    r = client.put(f"/sessions/{sid}/byproducts", json=body)
    assert r.status_code == 422
    assert client.get(f"/sessions/{sid}").json()["result"]["edited"] is None


def test_put_missing_fields(make):
    client, _ = make(vfp("V", ITEMS))
    sid = client.post("/sessions", json=BODY).json()["session_id"]
    r = client.put(f"/sessions/{sid}/byproducts", json={"vfp": "V", "byproducts": [{"name": "a"}]})
    assert r.status_code == 422 and r.json() == {"detail": "byproducts.0.description缺少"}


@pytest.mark.parametrize("bad", ["not json", '{"status":"vfp","vfp":"V"}', '{"status":"clarify"}'])
def test_unparseable_model_reply_is_502_not_saved_and_resendable(make, tmp_path, bad):
    client, _ = make(bad, clarify("Q"))
    r = client.post("/sessions", json=BODY)
    assert r.status_code == 502 and "無法解析" in r.json()["detail"]
    assert not list((tmp_path / "sessions").glob("*.json"))
    assert client.post("/sessions", json=BODY).status_code == 200  # 同一請求重送


def test_unparseable_reply_on_answer_keeps_session_and_can_resend(make):
    client, _ = make(clarify("Q1"), "not json", vfp("V", ITEMS))
    sid = client.post("/sessions", json=BODY).json()["session_id"]
    before = client.get(f"/sessions/{sid}").json()
    assert client.post(f"/sessions/{sid}/answer", json={"answer": "A1"}).status_code == 502
    assert client.get(f"/sessions/{sid}").json() == before
    assert client.post(f"/sessions/{sid}/answer", json={"answer": "A1"}).json()["status"] == "vfp"


def test_model_unavailable_is_503(tmp_path):
    def boom():
        raise RuntimeError("缺少環境變數")

    client = TestClient(create_app(CoachService(JsonFileStore(tmp_path / "sessions"), boom)))
    r = client.post("/sessions", json=BODY)
    assert r.status_code == 503 and "缺少環境變數" in r.json()["detail"]


def test_docs_page_and_endpoints_listed(make):
    client, _ = make()
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths) == {"/industries", "/sessions", "/sessions/{session_id}/answer", "/sessions/{session_id}/byproducts", "/sessions/{session_id}"}


# ---- 網頁 -----------------------------------------------------------------


def test_index_page_is_served_with_title_and_local_assets(make):
    client, _ = make()
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "管到飛" in r.text and "每週陪跑教練，一上任就帶大家起飛" in r.text
    assert "/static/style.css" in r.text and "/static/app.js" in r.text


def test_index_page_uses_no_external_resources(make):
    import re

    client, _ = make()
    for path in ("/", "/static/style.css", "/static/app.js"):
        text = client.get(path).text
        assert not re.search(r"(https?:)?//[\w.-]+\.[a-z]{2,}", re.sub(r"http://127\.0\.0\.1", "", text)), path


@pytest.mark.parametrize("path,kind", [("/static/style.css", "text/css"), ("/static/app.js", "javascript")])
def test_static_files_are_available(make, path, kind):
    client, _ = make()
    r = client.get(path)
    assert r.status_code == 200 and kind in r.headers["content-type"]


def test_css_defines_the_palette_variables_and_no_pure_black(make):
    client, _ = make()
    css = client.get("/static/style.css").text
    for name, value in {
        "--navy-900": "#0C3467", "--navy-800": "#0F325E", "--blue-700": "#1E5AA8", "--slate-700": "#2D4A6E",
        "--gray-600": "#5B6B80", "--gray-blue-400": "#8A9BB0", "--sky-300": "#9CC3EA", "--sky-100": "#DCEBFA",
        "--mist-100": "#E8EEF5", "--yellow-400": "#F4C84D",
    }.items():
        assert f"{name}: {value};" in css
    assert "#000" not in css.lower().replace("#0c3467", "").replace("#0f325e", "")


def test_missing_logo_is_just_404_and_page_still_loads(make):
    client, _ = make()
    assert client.get("/static/logo.png").status_code in (200, 404)
    assert client.get("/").status_code == 200


def test_api_routes_still_work_next_to_static_mount(make):
    client, _ = make()
    assert client.get("/industries").status_code == 200
    assert client.get("/docs").status_code == 200
