"""雲端部署相關：資料庫儲存層、設定檢查、限流 IP、健康檢查、錯誤訊息、匯入腳本。全部不需金鑰、不連網。"""

from __future__ import annotations

import json
import logging
import os
import threading

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

from coach import migrate, settings
from coach.api import create_app
from coach.db import create_db_engine
from coach.flow import CoachService
from coach.ratelimit import RateLimiter, client_ip
from coach.store import DbSessionStore, JsonFileStore, SessionNotFound, new_session_id
from scripts import import_json_sessions
from tests.fakes import SequenceLLM, clarify

BODY = {"name": "王主管", "industry": 9}


@pytest.fixture
def db_store(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    with engine.begin() as conn:
        migrate.upgrade_head(conn)
    return DbSessionStore(engine, sleep=lambda _: None)


def client_for(store, *replies, limiter=None):
    llm = SequenceLLM(*replies)
    return TestClient(create_app(CoachService(store, lambda: llm), limiter=limiter)), llm


# ---- 儲存層（SQLite）------------------------------------------------------


def test_store_create_read_update(db_store):
    sid = db_store.new_id()
    db_store.save({"id": sid, "x": "中文", "n": [1, 2]})
    assert db_store.load(sid) == {"id": sid, "x": "中文", "n": [1, 2]}
    db_store.save({"id": sid, "x": "改過", "n": []})
    assert db_store.load(sid)["x"] == "改過"
    assert len(db_store.list_all()) == 1  # 更新不會多一筆


def test_store_load_returns_independent_copy(db_store):
    sid = db_store.new_id()
    db_store.save({"id": sid, "a": {"b": 1}})
    db_store.load(sid)["a"]["b"] = 99
    assert db_store.load(sid)["a"]["b"] == 1


@pytest.mark.parametrize("bad", ["nope", "../x", "a/b", "", "a" * 65, "a.b"])
def test_store_unknown_or_unsafe_id_is_not_found(db_store, bad):
    with pytest.raises(SessionNotFound):
        db_store.load(bad)


def test_store_concurrent_updates_do_not_lose_rows_or_corrupt(db_store):
    sid = db_store.new_id()
    db_store.save({"id": sid, "v": 0})
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            for j in range(10):
                db_store.save({"id": sid, "v": i * 100 + j})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(db_store.list_all()) == 1
    assert db_store.load(sid)["v"] % 100 == 9  # 最後一筆一定是某個執行緒完整寫入的值


def test_store_concurrent_first_insert_same_id(db_store):
    sid = db_store.new_id()
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def worker(i: int) -> None:
        try:
            barrier.wait()
            db_store.save({"id": sid, "who": i})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(db_store.list_all()) == 1


def test_store_retries_transient_connection_errors(db_store):
    calls = {"n": 0}
    real = db_store._retry.__func__  # noqa: SLF001

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("SELECT", {}, Exception("connection closed"))
        return "ok"

    assert real(db_store, flaky) == "ok" and calls["n"] == 3
    delays: list[float] = []
    db_store._sleep = delays.append  # noqa: SLF001
    calls["n"] = -100
    with pytest.raises(OperationalError):
        real(db_store, flaky)
    assert delays == list(DbSessionStore.RETRY_DELAYS)


def test_session_ids_are_unguessable_and_unique():
    ids = {new_session_id() for _ in range(2000)}
    assert len(ids) == 2000
    assert all(len(i) >= 22 for i in ids)  # 16 位元組 = 128 位元
    assert JsonFileStore.__new__(JsonFileStore).new_id() != new_session_id()


# ---- migration / engine ---------------------------------------------------


def test_migration_creates_sessions_table_and_matches_models(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    with engine.begin() as conn:
        migrate.upgrade_head(conn)
    cols = {c["name"] for c in inspect(engine).get_columns("sessions")}
    assert cols == {"id", "created_at", "updated_at", "payload", "owner_user_id"}
    assert "users" not in inspect(engine).get_table_names()
    # 模型與 migration 沒有落差（有人改了模型卻忘了寫 migration 就會失敗）
    cfg = Config(str(settings.ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(settings.ROOT / "migrations"))
    cfg.attributes["skip_logging"] = True
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        command.check(cfg)


def test_postgres_engine_settings_without_connecting():
    e = create_db_engine("postgresql+psycopg://u:p@h.example/db")
    assert e.pool._pre_ping is True  # noqa: SLF001
    assert e.pool.size() == 5


def test_database_url_defaults_and_normalisation():
    assert settings.database_url({}).startswith("sqlite:///")
    assert settings.database_url({"DATABASE_URL": "postgres://u:p@h/db"}) == "postgresql+psycopg://u:p@h/db"
    assert settings.database_url({"DATABASE_URL": "postgresql://u:p@h/db"}) == "postgresql+psycopg://u:p@h/db"
    assert settings.migration_database_url({"DATABASE_URL": "postgres://pooled/db", "DATABASE_URL_DIRECT": "postgres://direct/db"}).endswith("direct/db")
    assert settings.migration_database_url({"DATABASE_URL": "postgres://pooled/db"}).endswith("pooled/db")


# ---- 設定檢查 -------------------------------------------------------------


def test_validate_is_silent_locally():
    settings.validate({})  # 本機什麼都沒設也不報錯


def test_validate_production_lists_missing_names_but_never_values():
    env = {"APP_ENV": "production", "AZURE_OPENAI_API_KEY": "SECRET-KEY-VALUE", "DATABASE_URL": ""}
    with pytest.raises(settings.SettingsError) as ei:
        settings.validate(env)
    msg = str(ei.value)
    assert "DATABASE_URL" in msg and "AZURE_OPENAI_ENDPOINT" in msg and "AZURE_OPENAI_DEPLOYMENT" in msg
    assert "AZURE_OPENAI_API_KEY" not in msg and "SECRET-KEY-VALUE" not in msg


def test_validate_production_rejects_sqlite_and_unknown_provider():
    ok = {
        "APP_ENV": "production",
        "AZURE_OPENAI_ENDPOINT": "e",
        "AZURE_OPENAI_API_KEY": "k",
        "AZURE_OPENAI_DEPLOYMENT": "d",
        "DATABASE_URL": "sqlite:///x.db",
    }
    with pytest.raises(settings.SettingsError, match="Postgres"):
        settings.validate(ok)
    settings.validate({**ok, "DATABASE_URL": "postgres://u:p@h/db"})
    with pytest.raises(settings.SettingsError, match="LLM_PROVIDER"):
        settings.validate({**ok, "LLM_PROVIDER": "nope"})


def test_validate_error_never_contains_connection_string():
    env = {"APP_ENV": "production", "DATABASE_URL": "sqlite:///pw-in-url.db"}
    with pytest.raises(settings.SettingsError) as ei:
        settings.validate(env)
    assert "pw-in-url" not in str(ei.value)


def test_app_startup_fails_clearly_in_production_when_misconfigured(monkeypatch):
    for k in ("DATABASE_URL", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    app = create_app(CoachService(DbSessionStore.__new__(DbSessionStore), lambda: None))
    with pytest.raises(settings.SettingsError, match="DATABASE_URL"):
        with TestClient(app):
            pass


# ---- 來源 IP 與限流 -------------------------------------------------------


def test_client_ip_legacy_mode_unchanged():
    assert client_ip({"X-Forwarded-For": "1.1.1.1, 10.0.0.1"}, "9.9.9.9") == "1.1.1.1"
    assert client_ip({"CF-Connecting-IP": "3.3.3.3", "X-Forwarded-For": "1.1.1.1"}, "9.9.9.9") == "3.3.3.3"


def test_client_ip_trusts_only_the_proxy_appended_entry():
    # 用戶端送假的 "6.6.6.6"，代理把真實來源 5.5.5.5 附加在尾端
    h = {"X-Forwarded-For": "6.6.6.6, 5.5.5.5"}
    assert client_ip(h, "10.0.0.1", trusted_hops=1) == "5.5.5.5"
    assert client_ip({"X-Forwarded-For": "6.6.6.6, 5.5.5.5, 172.70.0.1"}, "10.0.0.1", trusted_hops=2) == "5.5.5.5"
    assert client_ip({"X-Forwarded-For": "5.5.5.5"}, "10.0.0.1", trusted_hops=1) == "5.5.5.5"


def test_client_ip_trusted_mode_ignores_cf_header_and_falls_back_to_peer():
    assert client_ip({"CF-Connecting-IP": "7.7.7.7", "X-Forwarded-For": "5.5.5.5"}, "10.0.0.1", trusted_hops=1) == "5.5.5.5"
    assert client_ip({}, "10.0.0.1", trusted_hops=1) == "10.0.0.1"
    assert client_ip({"X-Forwarded-For": "5.5.5.5"}, "10.0.0.1", trusted_hops=2) == "10.0.0.1"  # 層數不夠＝繞過代理
    assert client_ip({}, None, trusted_hops=1) == "unknown"


def test_trusted_proxy_hops_setting():
    assert settings.trusted_proxy_hops({}) is None
    assert settings.trusted_proxy_hops({"APP_ENV": "production"}) == 1
    assert settings.trusted_proxy_hops({"APP_ENV": "production", "TRUSTED_PROXY_HOPS": "2"}) == 2
    assert settings.trusted_proxy_hops({"TRUSTED_PROXY_HOPS": "0"}) is None
    with pytest.raises(settings.SettingsError):
        settings.trusted_proxy_hops({"TRUSTED_PROXY_HOPS": "x"})


def test_rate_limit_cannot_be_bypassed_by_spoofed_forwarded_for(db_store, monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    client, _ = client_for(db_store, *[clarify("Q")] * 6, limiter=RateLimiter(ip_max=2))
    codes = [
        client.post("/sessions", json=BODY, headers={"X-Forwarded-For": f"9.9.9.{i}, 5.5.5.5"}).status_code
        for i in range(4)
    ]
    assert codes == [200, 200, 429, 429]  # 同一個真實來源，換假的最前面那個也沒用
    other = client.post("/sessions", json=BODY, headers={"X-Forwarded-For": "1.1.1.1, 5.5.5.6"})
    assert other.status_code == 200  # 另一個真實來源不受影響


# ---- 健康檢查、錯誤訊息、日誌 ----------------------------------------------


class ExplodingStore:
    def new_id(self):
        raise AssertionError("healthz 不該碰資料庫")

    load = save = new_id


def test_healthz_touches_neither_database_nor_model():
    def no_model():
        raise AssertionError("healthz 不該呼叫模型")

    client = TestClient(create_app(CoachService(ExplodingStore(), no_model)))
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_healthz_is_in_openapi_docs():
    client = TestClient(create_app(CoachService(ExplodingStore(), lambda: None)))
    assert "/healthz" in client.get("/openapi.json").json()["paths"]


def test_provider_error_detail_is_friendly_and_leaks_nothing(db_store):
    def boom():
        raise ConnectionError("https://secret-endpoint.example/openai?key=ABC123")

    client = TestClient(create_app(CoachService(db_store, boom)))
    r = client.post("/sessions", json=BODY)
    assert r.status_code == 503
    assert r.json() == {"detail": "目前無法連線到模型，請稍後再試"}


def test_bad_model_reply_detail_does_not_echo_model_text(db_store):
    client, _ = client_for(db_store, "機密原文 not json")
    r = client.post("/sessions", json=BODY)
    assert r.status_code == 502 and "無法解析" in r.json()["detail"]
    assert "機密原文" not in r.text


def test_database_failure_is_friendly_503_without_sql_or_params(db_store, monkeypatch):
    client, _ = client_for(db_store, clarify("Q"))

    def broken(*_a, **_k):
        raise OperationalError("INSERT INTO sessions ...", {"payload": "使用者的完整內容"}, Exception("password=hunter2"))

    monkeypatch.setattr(db_store, "save", broken)
    r = client.post("/sessions", json=BODY)
    assert r.status_code == 503
    assert r.json() == {"detail": "資料暫時無法存取，請稍後再試", "code": "db_unavailable"}
    for leaked in ("INSERT", "使用者的完整內容", "hunter2"):
        assert leaked not in r.text


def test_unexpected_error_is_friendly_500_and_logs_only_type(db_store, monkeypatch, caplog):
    client, _ = client_for(db_store)

    def broken(*_a, **_k):
        raise ValueError("secret 內容 postgresql://user:pw@host/db")

    monkeypatch.setattr(db_store, "load", broken)
    with caplog.at_level(logging.DEBUG):
        r = client.get("/sessions/abc")
    assert r.status_code == 500 and r.json() == {"detail": "系統發生錯誤，請稍後再試"}
    logged = caplog.text
    assert "ValueError" in logged and "pw@host" not in logged and "secret" not in logged


def test_logs_contain_session_id_and_status_but_not_user_input(db_store, caplog):
    client, _ = client_for(db_store, clarify("Q"))
    secret = {"name": "絕密稱呼", "industry": 9, "product": "絕密產品內容", "sales": "絕密銷售", "reason": "絕密原因"}
    with caplog.at_level(logging.DEBUG):
        sid = client.post("/sessions", json=secret).json()["session_id"]
    assert f"session={sid}" in caplog.text and "status=clarify" in caplog.text
    assert "絕密" not in caplog.text


def test_session_is_persisted_across_store_instances(tmp_path):
    """模擬「重新部署」：換一個全新的 store／app 連到同一個資料庫，案例還在。"""
    url = f"sqlite:///{(tmp_path / 'redeploy.db').as_posix()}"
    engine = create_db_engine(url)
    with engine.begin() as conn:
        migrate.upgrade_head(conn)
    client1, _ = client_for(DbSessionStore(engine), clarify("Q1"))
    sid = client1.post("/sessions", json=BODY).json()["session_id"]
    engine.dispose()
    client2, _ = client_for(DbSessionStore(create_db_engine(url)))
    r = client2.get(f"/sessions/{sid}")
    assert r.status_code == 200 and r.json()["status"] == "clarify"
    assert client2.get("/sessions/doesnotexist").status_code == 404


# ---- 舊 JSON 匯入 ---------------------------------------------------------


def _old_session(directory, sid, name="王主管"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{sid}.json").write_text(json.dumps({"id": sid, "inputs": {"name": name}}, ensure_ascii=False), encoding="utf-8")


def test_import_dry_run_then_apply_then_idempotent(tmp_path, db_store, capsys):
    src = tmp_path / "old"
    _old_session(src, "20260101-120000-abc123")
    _old_session(src, "20260101-120001-def456")
    (src / "broken.json").write_text("{not json", encoding="utf-8")

    assert import_json_sessions.main(["--dir", str(src)], store=db_store) == 0
    out = capsys.readouterr().out
    assert "可匯入：2 筆" in out and "無法讀取：1 筆" in out and "dry-run" in out
    assert db_store.list_all() == []  # dry-run 不寫入

    assert import_json_sessions.main(["--dir", str(src), "--apply"], store=db_store) == 0
    assert "已寫入：2 筆" in capsys.readouterr().out
    assert db_store.load("20260101-120000-abc123")["inputs"]["name"] == "王主管"

    import_json_sessions.main(["--dir", str(src), "--apply"], store=db_store)
    assert "已存在（略過）：2 筆" in capsys.readouterr().out
    assert len(db_store.list_all()) == 2


def test_import_with_no_old_files_is_not_an_error(tmp_path, db_store, capsys):
    assert import_json_sessions.main(["--dir", str(tmp_path / "missing")], store=db_store) == 0
    assert import_json_sessions.main(["--dir", str(tmp_path), "--apply"], store=db_store) == 0
    assert "可匯入：0 筆" in capsys.readouterr().out


def test_import_never_prints_user_content(tmp_path, db_store, capsys):
    _old_session(tmp_path / "old", "20260101-120000-abc123", name="絕密稱呼")
    import_json_sessions.main(["--dir", str(tmp_path / "old"), "--apply"], store=db_store)
    assert "絕密稱呼" not in capsys.readouterr().out


# ---- Postgres 整合測試（預設略過）----------------------------------------


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="設定 TEST_DATABASE_URL（一個可以隨意寫入的 Postgres）才會執行")
def test_postgres_integration_roundtrip_and_concurrency():
    from sqlalchemy import text

    engine = create_db_engine(settings.database_url({"DATABASE_URL": os.environ["TEST_DATABASE_URL"]}))
    with engine.begin() as conn:
        migrate.upgrade_head(conn)
    store = DbSessionStore(engine)
    sid = store.new_id()
    try:
        store.save({"id": sid, "x": "中文"})
        threads = [threading.Thread(target=lambda i=i: store.save({"id": sid, "x": i})) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert store.load(sid)["x"] in range(8)
        with pytest.raises(SessionNotFound):
            store.load("nope")
        with engine.connect() as conn:
            assert conn.execute(text("select data_type from information_schema.columns where table_name='sessions' and column_name='payload'")).scalar() == "jsonb"
    finally:
        with engine.begin() as conn:
            conn.execute(text("delete from sessions where id = :i"), {"i": sid})
