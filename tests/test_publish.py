"""發表用的對外功能：ngrok 通道、QR、啟動程式、限流、/qr 頁面、.bat、check 的對外檢查。全部不連網。"""

import py_compile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coach import launch, tunnel
from coach.api import create_app
from coach.flow import CoachService
from coach.ratelimit import RateLimiter, client_ip
from coach.store import JsonFileStore
from tests.fakes import SequenceLLM, clarify
from tests.test_check import FakeLLM, FakeTunnel, env, run, web_ok  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parent.parent
URL = "https://abc-123.ngrok-free.app"


def ngrok_payload(*tunnels):
    return {"tunnels": list(tunnels), "uri": "/api/tunnels"}


def tun(public_url, addr="http://localhost:8000"):
    return {"name": "x", "public_url": public_url, "proto": "https", "config": {"addr": addr}}


# ---- 解析 ngrok 管理介面的回應 --------------------------------------------------


def test_pick_https_url_prefers_https_for_port_8000():
    payload = ngrok_payload(tun("http://abc.ngrok-free.app"), tun(URL))
    assert tunnel.pick_https_url(payload) == URL


@pytest.mark.parametrize(
    "payload",
    [None, [], "x", {}, {"tunnels": None}, {"tunnels": []}, {"tunnels": ["x", 1, None]},
     {"tunnels": [{"public_url": "http://only-http"}]}, {"tunnels": [{"public_url": 5}]}],
)
def test_pick_https_url_none_for_unusable_payloads(payload):
    assert tunnel.pick_https_url(payload) is None


def test_pick_https_url_ignores_tunnels_to_other_ports():
    assert tunnel.pick_https_url(ngrok_payload(tun(URL, "http://localhost:3000"))) is None
    assert tunnel.pick_https_url(ngrok_payload(tun(URL, "http://localhost:80000"))) is None
    assert tunnel.pick_https_url(ngrok_payload(tun(URL, "localhost:8000"))) == URL
    assert tunnel.pick_https_url({"tunnels": [{"public_url": URL}]}) == URL  # 沒有 addr 資訊就不挑剔


@pytest.mark.parametrize(
    "log,code",
    [
        ("ERR_NGROK_4018 authentication failed: usage requires an authtoken", "auth"),
        ("lvl=eror msg='failed to auth' err='Your authtoken is invalid'", "auth"),
        ("dial tcp: lookup connect.ngrok-agent.com: no such host", "network"),
        ("failed to connect: i/o timeout", "network"),
        ("something else entirely", "exited"),
        ("", "exited"),
    ],
)
def test_classify_log(log, code):
    assert tunnel.classify_log(log) == code


@pytest.mark.parametrize("code", ["not_found", "auth", "network", "timeout", "exited"])
def test_every_failure_reason_has_chinese_text_with_a_hint(code):
    text = tunnel.explain_reason(code)
    assert text and "。" in text and any("\u4e00" <= c <= "\u9fff" for c in text)
    assert tunnel.explain_reason("auth").count("authtoken") >= 1 and "熱點" in tunnel.explain_reason("network")


# ---- 網址檔與 QR 圖檔 -----------------------------------------------------------


def test_make_qr_svg_is_inline_svg_without_pillow_or_xml_header():
    svg = tunnel.make_qr_svg(URL)
    assert svg.startswith("<svg") and 'width="100%"' in svg and "<?xml" not in svg
    assert "mm" in tunnel.make_qr_svg(URL, size=None)


def test_write_public_url_writes_both_files_and_overwrites(tmp_path):
    d = tmp_path / "qr"
    tunnel.write_public_url(URL, d)
    assert (d / "public_url.txt").read_text(encoding="utf-8").strip() == URL
    first_svg = (d / "qr.svg").read_text(encoding="utf-8")
    assert first_svg.startswith("<svg")
    tunnel.write_public_url("https://second.ngrok-free.app", d)
    assert (d / "public_url.txt").read_text(encoding="utf-8").strip() == "https://second.ngrok-free.app"
    assert (d / "qr.svg").read_text(encoding="utf-8") != first_svg  # 圖也換成新的
    assert sorted(p.name for p in d.iterdir()) == ["public_url.txt", "qr.svg"]


def test_clear_qr_dir_removes_old_files_and_tolerates_missing_dir(tmp_path):
    d = tmp_path / "qr"
    d.mkdir()
    (d / "public_url.txt").write_text("old", encoding="utf-8")
    (d / "qr.svg").write_text("old", encoding="utf-8")
    tunnel.clear_qr_dir(d)
    assert not d.exists()
    tunnel.clear_qr_dir(d)  # 不存在也不報錯


def test_read_public_url_priority_file_then_env_then_none(tmp_path):
    assert tunnel.read_public_url(tmp_path, env={}) is None
    assert tunnel.read_public_url(tmp_path, env={"PUBLIC_URL": " https://env.example/ "}) == "https://env.example/"
    (tmp_path / "public_url.txt").write_text(URL + "\n", encoding="utf-8")
    assert tunnel.read_public_url(tmp_path, env={"PUBLIC_URL": "https://env.example/"}) == URL
    (tmp_path / "public_url.txt").write_text("  \n", encoding="utf-8")  # 空檔案視同沒有
    assert tunnel.read_public_url(tmp_path, env={"PUBLIC_URL": "https://env.example/"}) == "https://env.example/"


# ---- NgrokTunnel（替身：不啟動程序、不連網）---------------------------------------


class FakeProc:
    def __init__(self, exit_code=None, hang=False):
        self.exit_code, self.hang = exit_code, hang
        self.terminated = self.killed = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        if not self.hang:
            self.exit_code = 0

    def kill(self):
        self.killed = True
        self.exit_code = -9

    def wait(self, timeout=None):
        if self.exit_code is None:
            raise TimeoutError("still running")
        return self.exit_code


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


def make_tunnel(tmp_path, *, proc=None, fetch=None, which=lambda n: "C:/ngrok.exe", log_text=None, **kw):
    clock = Clock()
    calls = []
    proc = proc or FakeProc()
    log_path = tmp_path / "ngrok.log"

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        if log_text is not None:
            log_path.write_text(log_text, encoding="utf-8")
        return proc

    t = tunnel.NgrokTunnel(
        8000, which=which, popen=popen, fetch=fetch or (lambda port: None), clock=clock, sleep=clock.sleep, log_path=log_path, **kw
    )
    return t, proc, calls, clock


def test_tunnel_success_polls_4040_then_4041(tmp_path):
    seen = []

    def fetch(port):
        seen.append(port)
        return ngrok_payload(tun(URL)) if port == 4041 else None

    t, proc, calls, _ = make_tunnel(tmp_path, fetch=fetch)
    result = t.start()
    assert result.url == URL and result.reason is None
    assert seen[:2] == [4040, 4041]
    args, kwargs = calls[0]
    assert args == ["C:/ngrok.exe", "http", "8000", "--log=stdout"]  # 輸出導向記錄檔，不佔畫面
    assert kwargs["stdout"] is not None and kwargs["stderr"] is not None
    assert t.is_running()
    t.stop()
    assert proc.terminated and not t.is_running()


def test_tunnel_ngrok_missing(tmp_path):
    t, _, calls, _ = make_tunnel(tmp_path, which=lambda n: None)
    result = t.start()
    assert (result.url, result.reason) == (None, "not_found") and calls == []


def test_tunnel_timeout_after_15_seconds(tmp_path):
    t, _, _, clock = make_tunnel(tmp_path, log_text="")
    result = t.start()
    assert (result.url, result.reason) == (None, "timeout")
    assert 15 <= clock.now < 17


def test_tunnel_process_exit_with_auth_error_fails_fast(tmp_path):
    t, _, _, clock = make_tunnel(tmp_path, proc=FakeProc(exit_code=1), log_text="ERR_NGROK_4018 authentication failed: authtoken")
    result = t.start()
    assert (result.url, result.reason) == (None, "auth")
    assert clock.now == 0  # 程序已經結束就不用等到逾時


def test_tunnel_network_error_reported_when_waiting_times_out(tmp_path):
    t, _, _, _ = make_tunnel(tmp_path, log_text="dial tcp: lookup connect.ngrok-agent.com: no such host")
    assert t.start().reason == "network"


def test_tunnel_ignores_other_programs_tunnels(tmp_path):
    t, _, _, _ = make_tunnel(tmp_path, fetch=lambda port: ngrok_payload(tun(URL, "http://localhost:3000")), log_text="")
    assert t.start().reason == "timeout"


def test_tunnel_stop_kills_when_terminate_is_not_enough(tmp_path):
    t, proc, _, _ = make_tunnel(tmp_path, proc=FakeProc(hang=True), fetch=lambda p: ngrok_payload(tun(URL)))
    t.start()
    t.stop()
    assert proc.terminated and proc.killed and not t.is_running()
    t.stop()  # 重複呼叫沒事


def test_ngrok_version(tmp_path):
    class R:
        returncode, stdout = 0, "ngrok version 3.0.0\n"

    assert tunnel.ngrok_version(run=lambda *a, **k: R(), which=lambda n: "x") == "ngrok version 3.0.0"
    assert tunnel.ngrok_version(which=lambda n: None) is None

    class Bad:
        returncode, stdout = 1, ""

    assert tunnel.ngrok_version(run=lambda *a, **k: Bad(), which=lambda n: "x") is None


# ---- 啟動程式 --------------------------------------------------------------------


class FakeServer:
    def __init__(self):
        self.exit_code = None
        self.terminated = self.killed = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = 0

    def kill(self):
        self.killed = True
        self.exit_code = -9

    def wait(self, timeout=None):
        return self.exit_code


class LaunchEnv:
    """把 launch.Deps 的每個元件換成替身，並記下發生的順序。"""

    def __init__(self, tmp_path, *, busy=False, ready=True, tunnel_obj=None, wait_raises=None):
        self.events, self.opened = [], []
        self.server = FakeServer()
        self.tunnel = tunnel_obj
        self.qr_dir = tmp_path / "qr"
        self.logs = []

        def spawn():
            self.events.append("spawn")
            return self.server

        def factory():
            self.events.append("tunnel_factory")
            if isinstance(self.tunnel, Exception):
                raise self.tunnel
            return self.tunnel

        def wait(server):
            self.events.append("wait")
            if wait_raises:
                raise wait_raises

        self.deps = launch.Deps(
            port_in_use=lambda p: busy,
            spawn_server=spawn,
            wait_for_port=lambda p, s: ready,
            tunnel_factory=factory,
            open_url=lambda u: self.opened.append(u),
            wait=wait,
            qr_dir=self.qr_dir,
            log=self.logs.append,
        )

    @property
    def text(self):
        return "\n".join(self.logs)


def test_launch_success_writes_files_prints_url_and_opens_both_tabs_in_order(tmp_path):
    e = LaunchEnv(tmp_path, tunnel_obj=FakeTunnel(URL))
    assert launch.run(deps=e.deps) == 0
    assert e.opened == ["http://127.0.0.1:8000/", "http://127.0.0.1:8000/qr"]
    assert (e.qr_dir / "public_url.txt").read_text(encoding="utf-8").strip() == URL
    assert (e.qr_dir / "qr.svg").is_file()
    assert URL in e.text


def test_launch_clears_old_qr_files_first_even_when_it_fails_later(tmp_path):
    e = LaunchEnv(tmp_path, busy=True)
    e.qr_dir.mkdir()
    (e.qr_dir / "public_url.txt").write_text("https://stale.example", encoding="utf-8")
    (e.qr_dir / "qr.svg").write_text("<svg/>", encoding="utf-8")
    assert launch.run(deps=e.deps) == 1
    assert not e.qr_dir.exists()


def test_launch_overwrites_old_url_with_new(tmp_path):
    e = LaunchEnv(tmp_path, tunnel_obj=FakeTunnel(URL))
    e.qr_dir.mkdir()
    (e.qr_dir / "public_url.txt").write_text("https://stale.example", encoding="utf-8")
    launch.run(deps=e.deps)
    assert (e.qr_dir / "public_url.txt").read_text(encoding="utf-8").strip() == URL


def test_launch_port_in_use_stops_with_chinese_message_and_starts_nothing(tmp_path):
    e = LaunchEnv(tmp_path, busy=True, tunnel_obj=FakeTunnel())
    assert launch.run(deps=e.deps) == 1
    assert "請先關閉舊的視窗" in e.text
    assert e.events == [] and e.opened == []


def test_launch_without_public_url_keeps_running_and_opens_only_home(tmp_path):
    t = FakeTunnel(url=None, reason="auth")
    e = LaunchEnv(tmp_path, tunnel_obj=t)
    assert launch.run(deps=e.deps) == 0  # 不中止
    assert e.events == ["spawn", "tunnel_factory", "wait"]  # 伺服器照常運作（一直等到使用者結束）
    assert e.opened == ["http://127.0.0.1:8000/"]
    assert not e.qr_dir.exists()
    assert "目前沒有公開網址" in e.text and "authtoken" in e.text
    assert t.stopped and e.server.terminated


@pytest.mark.parametrize("reason", ["not_found", "network", "timeout", "exited"])
def test_launch_every_failure_reason_prints_hint_without_aborting(tmp_path, reason):
    e = LaunchEnv(tmp_path, tunnel_obj=FakeTunnel(url=None, reason=reason))
    assert launch.run(deps=e.deps) == 0
    assert tunnel.explain_reason(reason) in e.text and e.opened == ["http://127.0.0.1:8000/"]


def test_launch_survives_tunnel_crash(tmp_path):
    class Boom(FakeTunnel):
        def start(self):
            raise RuntimeError("ngrok exploded")

    e = LaunchEnv(tmp_path, tunnel_obj=Boom())
    assert launch.run(deps=e.deps) == 0
    assert "wait" in e.events and e.opened == ["http://127.0.0.1:8000/"] and e.server.terminated


def test_launch_survives_tunnel_factory_crash(tmp_path):
    e = LaunchEnv(tmp_path, tunnel_obj=OSError("no ngrok"))
    assert launch.run(deps=e.deps) == 0 and "wait" in e.events


def test_launch_no_tunnel_never_creates_a_tunnel(tmp_path):
    e = LaunchEnv(tmp_path, tunnel_obj=FakeTunnel(URL))
    assert launch.run(no_tunnel=True, deps=e.deps) == 0
    assert "tunnel_factory" not in e.events and e.opened == ["http://127.0.0.1:8000/"]
    assert not e.qr_dir.exists()


def test_launch_main_parses_no_tunnel_flag(tmp_path):
    e = LaunchEnv(tmp_path, tunnel_obj=FakeTunnel(URL))
    assert launch.main(["--no-tunnel"], deps=e.deps) == 0
    assert "tunnel_factory" not in e.events
    e2 = LaunchEnv(tmp_path, tunnel_obj=FakeTunnel(URL))
    assert launch.main([], deps=e2.deps) == 0 and "tunnel_factory" in e2.events


def test_launch_stops_both_children_on_normal_exit(tmp_path):
    t = FakeTunnel(URL)
    e = LaunchEnv(tmp_path, tunnel_obj=t)
    launch.run(deps=e.deps)
    assert t.stopped and e.server.terminated


def test_launch_stops_both_children_on_ctrl_c(tmp_path):
    t = FakeTunnel(URL)
    e = LaunchEnv(tmp_path, tunnel_obj=t, wait_raises=KeyboardInterrupt())
    assert launch.run(deps=e.deps) == 0
    assert t.stopped and e.server.terminated


def test_launch_stops_server_when_it_never_becomes_ready(tmp_path):
    t = FakeTunnel(URL)
    e = LaunchEnv(tmp_path, ready=False, tunnel_obj=t)
    assert launch.run(deps=e.deps) == 1
    assert e.server.terminated and "tunnel_factory" not in e.events and e.opened == []


def test_stop_process_terminates_then_kills_if_needed():
    p = FakeServer()
    launch.stop_process(p)
    assert p.terminated and not p.killed
    stubborn = FakeProc(hang=True)
    launch.stop_process(stubborn, timeout=0.01)
    assert stubborn.terminated and stubborn.killed
    launch.stop_process(None)  # 沒有程序也沒事
    launch.stop_process(FakeServer())


def test_real_launch_spawns_uvicorn_without_reload():
    import inspect

    src = inspect.getsource(launch.spawn_server)
    assert "uvicorn" in src and "--reload" not in src.replace("# 不用 --reload", "")


# ---- 限流與來源 IP ----------------------------------------------------------------


def test_client_ip_priority():
    peer = "127.0.0.1"
    assert client_ip({}, peer) == "127.0.0.1"
    assert client_ip({"X-Forwarded-For": "1.1.1.1, 10.0.0.1"}, peer) == "1.1.1.1"
    assert client_ip({"x-forwarded-for": " 2.2.2.2 "}, peer) == "2.2.2.2"
    assert client_ip({"CF-Connecting-IP": "3.3.3.3", "X-Forwarded-For": "1.1.1.1"}, peer) == "3.3.3.3"
    assert client_ip({"cf-connecting-ip": " ", "X-Forwarded-For": "1.1.1.1"}, peer) == "1.1.1.1"  # 空值視同沒有
    assert client_ip({}, None) == "unknown"


def test_per_ip_limit_30_per_10_minutes_with_window_expiry():
    now = [0.0]
    lim = RateLimiter(clock=lambda: now[0])
    assert all(lim.allow("a") for _ in range(30))
    assert lim.allow("a") is False
    assert lim.allow("b") is True  # 別的來源不受影響
    now[0] = 599
    assert lim.allow("a") is False
    now[0] = 601  # 10 分鐘之後恢復
    assert lim.allow("a") is True


def test_global_limit_300_per_hour():
    now = [0.0]
    lim = RateLimiter(clock=lambda: now[0])
    for i in range(300):
        assert lim.allow(f"ip{i}")  # 每個來源只用一次，不會碰到單一來源上限
    assert lim.allow("fresh") is False
    now[0] = 3601
    assert lim.allow("fresh") is True


def test_blocked_requests_do_not_extend_the_block():
    now = [0.0]
    lim = RateLimiter(ip_max=2, clock=lambda: now[0])
    assert lim.allow("a") and lim.allow("a")
    for t in (100, 200, 300, 400, 500):
        now[0] = t
        assert lim.allow("a") is False
    now[0] = 601
    assert lim.allow("a") is True


def test_limits_come_from_env_with_defaults_and_bad_values_ignored():
    lim = RateLimiter.from_env({})
    assert (lim.ip_max, lim.ip_window, lim.global_max, lim.global_window) == (30, 600, 300, 3600)
    lim = RateLimiter.from_env(
        {"RATE_LIMIT_IP_MAX": "5", "RATE_LIMIT_IP_WINDOW_SECONDS": "60", "RATE_LIMIT_GLOBAL_MAX": "7", "RATE_LIMIT_GLOBAL_WINDOW_SECONDS": "120"}
    )
    assert (lim.ip_max, lim.ip_window, lim.global_max, lim.global_window) == (5, 60, 7, 120)
    lim = RateLimiter.from_env({"RATE_LIMIT_IP_MAX": "abc", "RATE_LIMIT_GLOBAL_MAX": "-3"})
    assert (lim.ip_max, lim.global_max) == (30, 300)


BODY = {"name": "王主管", "industry": 9}


def limited_client(tmp_path, limiter, *replies):
    llm = SequenceLLM(*replies)
    svc = CoachService(JsonFileStore(tmp_path / "sessions"), lambda: llm)
    return TestClient(create_app(svc, limiter=limiter, qr_dir=tmp_path / "qr")), llm


def test_model_endpoints_return_429_in_chinese_after_limit(tmp_path):
    client, llm = limited_client(tmp_path, RateLimiter(ip_max=2), clarify("Q"), clarify("Q"), clarify("Q"))
    assert client.post("/sessions", json=BODY).status_code == 200
    sid = client.post("/sessions", json=BODY).json()["session_id"]
    r = client.post("/sessions", json=BODY)
    assert r.status_code == 429 and r.json() == {"detail": "目前使用的人比較多，請稍後再試"}
    r = client.post(f"/sessions/{sid}/answer", json={"answer": "x"})  # /answer 也共用同一個額度
    assert r.status_code == 429
    assert len(llm.calls) == 2  # 被擋的請求不會呼叫模型


def test_get_endpoints_are_not_limited(tmp_path):
    client, _ = limited_client(tmp_path, RateLimiter(ip_max=1), clarify("Q"))
    sid = client.post("/sessions", json=BODY).json()["session_id"]
    for _ in range(5):
        assert client.get(f"/sessions/{sid}").status_code == 200
        assert client.get("/industries").status_code == 200
        assert client.get("/qr").status_code == 200


def test_limit_uses_forwarded_ip_not_the_tunnel_connection_address(tmp_path):
    client, _ = limited_client(tmp_path, RateLimiter(ip_max=1), clarify("Q"), clarify("Q"), clarify("Q"))
    a = {"X-Forwarded-For": "1.1.1.1"}
    b = {"X-Forwarded-For": "2.2.2.2"}
    assert client.post("/sessions", json=BODY, headers=a).status_code == 200
    assert client.post("/sessions", json=BODY, headers=a).status_code == 429
    assert client.post("/sessions", json=BODY, headers=b).status_code == 200  # 同一條通道、不同使用者
    cf = {"CF-Connecting-IP": "3.3.3.3", "X-Forwarded-For": "1.1.1.1"}
    assert client.post("/sessions", json=BODY, headers=cf).status_code == 200  # CF 標頭優先


def test_frontend_shows_429_message():
    js = (ROOT / "coach" / "web" / "app.js").read_text(encoding="utf-8")
    assert "res.status === 429" in js and "目前使用的人比較多，請稍後再試" in js


# ---- /qr 頁面 ---------------------------------------------------------------------


@pytest.fixture
def qr_client(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    client, _ = limited_client(tmp_path, RateLimiter())
    return client, tmp_path / "qr"


def test_qr_page_without_file_or_env_says_no_public_url(qr_client):
    client, _ = qr_client
    r = client.get("/qr")
    assert r.status_code == 200 and "目前沒有公開網址" in r.text and "<svg" not in r.text
    assert "掃描就能試用：管到飛" in r.text


def test_qr_page_with_file_shows_svg_url_title_and_note(qr_client):
    client, qr = qr_client
    tunnel.write_public_url(URL, qr)
    r = client.get("/qr")
    assert r.status_code == 200
    assert "<svg" in r.text and URL in r.text and "目前沒有公開網址" not in r.text
    assert "掃描就能試用：管到飛" in r.text and "第一次開啟若看到 ngrok 的提醒頁，請按 Visit Site" in r.text
    assert "min-width: 360px" in r.text  # QR 放大（360px 以上）
    assert "background: #FFFFFF" in r.text


def test_qr_page_with_env_only(qr_client, monkeypatch):
    client, _ = qr_client
    monkeypatch.setenv("PUBLIC_URL", "https://from-env.example.com")
    r = client.get("/qr")
    assert "https://from-env.example.com" in r.text and "<svg" in r.text


def test_qr_page_file_beats_env(qr_client, monkeypatch):
    client, qr = qr_client
    monkeypatch.setenv("PUBLIC_URL", "https://from-env.example.com")
    tunnel.write_public_url(URL, qr)
    text = client.get("/qr").text
    assert URL in text and "from-env" not in text


def test_qr_page_reads_latest_file_every_time_and_is_not_cached(qr_client):
    client, qr = qr_client
    tunnel.write_public_url("https://first.ngrok-free.app", qr)
    r1 = client.get("/qr")
    assert "no-store" in r1.headers["cache-control"]
    tunnel.write_public_url("https://second.ngrok-free.app", qr)
    assert "second.ngrok-free.app" in client.get("/qr").text
    tunnel.clear_qr_dir(qr)
    assert "目前沒有公開網址" in client.get("/qr").text


def test_qr_page_has_no_external_resources_and_escapes_url(qr_client):
    import re

    client, qr = qr_client
    tunnel.write_public_url(URL, qr)
    text = client.get("/qr").text
    assert not re.search(r"<(script|link|img|iframe)\b", text) and "@import" not in text and "src=" not in text
    assert [u for u in re.findall(r"https?://[^\s\"'<>]+", text) if u != URL and not u.startswith("http://www.w3.org/")] == []
    (qr / "public_url.txt").write_text('https://x.test/"><script>alert(1)</script>', encoding="utf-8")
    assert "<script>" not in client.get("/qr").text


# ---- .bat -------------------------------------------------------------------------


def _bat(name):
    raw = (ROOT / "bat" / name).read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")  # CRLF
    return raw.decode("utf-8")


@pytest.mark.parametrize("name,flag", [("run.bat", False), ("run_local.bat", True)])
def test_run_bats(name, flag):
    t = _bat(name)
    assert "chcp 65001" in t and 'cd /d "%~dp0.."' in t
    assert ".venv\\Scripts\\activate.bat" in t and "找不到 .venv" in t
    lines = [ln.strip() for ln in t.splitlines()]
    assert ("python -m coach.launch --no-tunnel" if flag else "python -m coach.launch") in lines
    assert t.rstrip().endswith("pause") and t.count("pause") >= 2
    assert "同一台電腦重複開多個 run.bat" in t


def test_check_bat_still_runs_check():
    t = _bat("check.bat")
    assert "chcp 65001" in t and "python -m coach.check" in t


def test_no_tunnel_bat():
    assert not list(ROOT.rglob("tunnel.bat")) or all(".venv" in str(p) for p in ROOT.rglob("tunnel.bat"))
    assert sorted(p.name for p in (ROOT / "bat").glob("*.bat")) == ["check.bat", "run.bat", "run_local.bat"]


# ---- check 的對外功能檢查 -------------------------------------------------------------


def test_check_outbound_all_ok_prints_url_and_no_leftover(env, capsys, web_ok):  # noqa: F811
    env()
    t = FakeTunnel("https://live.ngrok-free.app")
    code, out, _ = run(capsys, web=web_ok, env_file=ROOT / "coach_rules.md", tunnel_factory=lambda: t)
    assert code == 0
    assert "[OK] ngrok：ngrok version 3.0.0" in out and "[OK] qrcode 套件" in out
    assert "取得公開網址 https://live.ngrok-free.app" in out and "沒有殘留程序" in out
    assert t.started and t.stopped  # 印出網址後立刻關閉通道


def test_check_ngrok_missing_is_reminded_and_affects_exit_code_only(env, capsys, web_ok):  # noqa: F811
    env()
    code, out, _ = run(capsys, web=web_ok, env_file=ROOT / "coach_rules.md", ngrok_version_fn=lambda: None)
    assert code == 1
    assert "[提醒] ngrok：找不到 ngrok" in out
    assert "[OK] 模型連線：成功" in out and "[OK] 網頁檔案：齊全" in out  # 既有項目的結論不變


def test_check_qrcode_missing(env, capsys, web_ok):  # noqa: F811
    env()
    code, out, _ = run(capsys, web=web_ok, env_file=ROOT / "coach_rules.md", qrcode_probe=lambda: False)
    assert code == 1 and "[提醒] qrcode 套件：不可用" in out and "pip install -r requirements.txt" in out


@pytest.mark.parametrize("reason,expected", [("auth", "authtoken"), ("network", "熱點"), ("timeout", "熱點"), ("exited", "ngrok http 8000")])
def test_check_tunnel_failures_explain_reason(env, capsys, web_ok, reason, expected):  # noqa: F811
    env()
    t = FakeTunnel(url=None, reason=reason)
    code, out, _ = run(capsys, web=web_ok, env_file=ROOT / "coach_rules.md", tunnel_factory=lambda: t)
    assert code == 1
    assert "[提醒] 對外通道實測：取不到公開網址" in out and expected in out
    assert t.stopped


def test_check_skips_tunnel_when_port_is_busy_without_failing(env, capsys, web_ok):  # noqa: F811
    env()
    started = []

    def factory():
        started.append(1)
        return FakeTunnel()

    code, out, _ = run(capsys, web=web_ok, port_busy=True, env_file=ROOT / "coach_rules.md", tunnel_factory=factory)
    assert started == []
    assert "對外通道實測：略過：8000 已被占用" in out
    assert "[提醒] 8000 連接埠" in out  # 既有的連接埠提醒照舊


def test_check_reports_leftover_process(env, capsys, web_ok):  # noqa: F811
    env()

    class Stuck(FakeTunnel):
        def is_running(self):
            return True

    code, out, _ = run(capsys, web=web_ok, env_file=ROOT / "coach_rules.md", tunnel_factory=Stuck)
    assert code == 1 and "[提醒] 通道已關閉：ngrok 還在執行" in out


def test_check_tunnel_crash_is_contained(env, capsys, web_ok):  # noqa: F811
    env()

    class Boom(FakeTunnel):
        def start(self):
            raise RuntimeError("boom")

    code, out, _ = run(capsys, web=web_ok, env_file=ROOT / "coach_rules.md", tunnel_factory=Boom)
    assert code == 1 and "對外通道實測：取不到公開網址" in out


# ---- 新增程式的語法檢查 ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["tunnel", "launch", "ratelimit", "check", "api", "export"])
def test_new_modules_compile(name, tmp_path):
    py_compile.compile(str(ROOT / "coach" / f"{name}.py"), cfile=str(tmp_path / f"{name}.pyc"), doraise=True)
