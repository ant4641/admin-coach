"""python -m coach.check、.bat 檔、VS Code 任務。全部不連網：模型用替身。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coach import check
from coach.llm import LLMResult
from coach.rules import load_rules

ROOT = Path(__file__).resolve().parent.parent
SECRET = "sk-SUPERSECRET-123456"

ENV_NAMES = [
    "LLM_PROVIDER", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_DEPLOYMENT", "OPENAI_API_KEY", "OPENAI_MODEL",
]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """乾淨的環境：清掉所有相關變數，給一個不存在的 .env，避免讀到真的金鑰。"""
    for n in ENV_NAMES:
        monkeypatch.delenv(n, raising=False)

    def full():
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid/")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", SECRET)
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "my-deploy")

    return full


class FakeLLM:
    provider = "azure"
    model = "my-deploy"

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []

    def complete(self, messages, max_tokens=None, timeout=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "timeout": timeout})
        if self.error:
            raise self.error
        return LLMResult(text='{"ok": true}', model="m", latency_ms=1)


class HttpError(Exception):
    def __init__(self, status_code, message="boom"):
        super().__init__(message)
        self.status_code = status_code


class APITimeoutError(Exception):
    pass


class APIConnectionError(Exception):
    pass


class FakeTunnel:
    """對外通道的替身：不啟動 ngrok、不連網。"""

    def __init__(self, url="https://abc.ngrok-free.app", reason=None):
        self.url, self.reason, self.started, self.stopped = url, reason, False, False

    def start(self):
        from coach.tunnel import TunnelResult

        self.started = True
        return TunnelResult(self.url, self.reason)

    def stop(self):
        self.stopped = True

    def is_running(self):
        return False


def run(capsys, llm=None, port_busy=False, web=check.WEB, env_file=None, factory=None, **outbound):
    """outbound：ngrok_version_fn / qrcode_probe / tunnel_factory；預設是「全部正常」的替身，不會連網。"""
    llm = llm or FakeLLM()
    outbound.setdefault("ngrok_version_fn", lambda: "ngrok version 3.0.0")
    outbound.setdefault("qrcode_probe", lambda: True)
    outbound.setdefault("tunnel_factory", FakeTunnel)
    code = check.main(
        [],
        llm_factory=factory or (lambda: llm),
        port_probe=lambda p: port_busy,
        env_file=env_file or Path("/nonexistent/.env"),
        web=web,
        **outbound,
    )
    return code, capsys.readouterr().out, llm


@pytest.fixture
def web_ok(tmp_path):
    """複製一份網頁檔案，含 sky.jpg 與 logo.png（真實資料夾可能沒有它們）。"""
    dst = tmp_path / "web"
    shutil.copytree(check.WEB, dst)
    for f in check.OPTIONAL_ASSETS:
        (dst / f).parent.mkdir(parents=True, exist_ok=True)
        (dst / f).touch()
    return dst


# ---- 成功 -----------------------------------------------------------------


def test_success_prints_rules_version_deployment_latency_and_exits_0(env, capsys, web_ok):
    env()
    code, out, llm = run(capsys, web=web_ok, env_file=ROOT / "coach_rules.md")  # 只要檔案存在即可
    rules = load_rules()
    assert code == 0
    assert f"{rules.version}" in out and rules.sha256[:8] in out
    assert "模型部署：my-deploy" in out
    assert "成功，延遲" in out and "ms" in out
    assert "全部 OK" in out and "提醒" not in out.replace("標示「提醒」", "")


def test_request_is_minimal(env, capsys, web_ok):
    env()
    _, _, llm = run(capsys, web=web_ok, env_file=ROOT / "coach_rules.md")
    (call,) = llm.calls
    assert call["max_tokens"] is not None and call["max_tokens"] <= 32
    assert call["timeout"] is not None
    assert len(json.dumps(call["messages"], ensure_ascii=False)) < 100
    assert "JSON" in call["messages"][0]["content"]  # json_object 模式要求訊息裡出現 JSON


# ---- 失敗訊息 -------------------------------------------------------------


@pytest.mark.parametrize(
    "error,expected",
    [
        (HttpError(401), "金鑰錯誤（401）"),
        (HttpError(404), "部署名稱"),
        (HttpError(400), "API 版本（例如 2024-10-21），不是模型版本"),
        (HttpError(403), "沒有權限（403）"),
        (HttpError(429), "限流（429）"),
        (HttpError(500), "伺服器回應錯誤（500）"),
        (APITimeoutError("timed out"), "逾時"),
        (APIConnectionError("no route"), "連線失敗"),
        (ConnectionError("reset"), "連線失敗"),
        (ValueError("???"), "無法完成請求（ValueError）"),
    ],
)
def test_failures_have_short_chinese_hints_and_exit_1(env, capsys, web_ok, error, expected):
    env()
    code, out, _ = run(capsys, llm=FakeLLM(error), web=web_ok, env_file=ROOT / "coach_rules.md")
    assert code == 1
    assert "[提醒] 模型連線：失敗" in out and expected in out


def test_missing_env_names_the_missing_field(env, capsys, web_ok, monkeypatch):
    env()
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT")
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT")
    code, out, llm = run(capsys, web=web_ok, env_file=ROOT / "coach_rules.md")
    assert code == 1 and llm.calls == []  # 缺欄位時不發請求
    assert "AZURE_OPENAI_DEPLOYMENT" in out and "AZURE_OPENAI_ENDPOINT" in out and "AZURE_OPENAI_API_KEY" not in out


def test_missing_env_file_is_reported(env, capsys, web_ok):
    env()
    code, out, _ = run(capsys, web=web_ok)  # env_file 不存在
    assert code == 1 and "找不到 .env" in out


def test_unknown_provider(env, capsys, web_ok, monkeypatch):
    env()
    monkeypatch.setenv("LLM_PROVIDER", "foo")
    code, out, _ = run(capsys, web=web_ok, env_file=ROOT / "coach_rules.md")
    assert code == 1 and "LLM_PROVIDER 不認得：foo" in out


def test_api_key_never_printed_even_if_error_message_contains_it(env, capsys, web_ok):
    env()
    err = HttpError(401, f"Incorrect API key provided: {SECRET}. Authorization: Bearer {SECRET}")
    code, out, _ = run(capsys, llm=FakeLLM(err), web=web_ok, env_file=ROOT / "coach_rules.md")
    assert code == 1
    assert SECRET not in out and "SUPERSECRET" not in out
    assert "***" in out  # 原始訊息裡的金鑰被遮掉


def test_api_key_not_printed_on_success_or_factory_failure(env, capsys, web_ok):
    env()
    _, out, _ = run(capsys, web=web_ok, env_file=ROOT / "coach_rules.md")
    assert SECRET not in out

    def broken():
        raise RuntimeError(f"bad config {SECRET}")

    code, out, _ = run(capsys, web=web_ok, env_file=ROOT / "coach_rules.md", factory=broken)
    assert code == 1 and SECRET not in out


# ---- 檔案與連接埠 ----------------------------------------------------------


def test_missing_sky_and_logo_are_reminded(env, capsys, web_ok):
    env()
    (web_ok / "assets" / "sky.jpg").unlink()
    code, out, _ = run(capsys, web=web_ok, env_file=ROOT / "coach_rules.md")
    assert code == 1
    assert "[提醒] coach/web/assets/sky.jpg" in out and "[OK] coach/web/assets/logo.png" in out


def test_missing_web_file_is_reported_by_name(env, capsys, web_ok):
    env()
    (web_ok / "app.js").unlink()
    code, out, _ = run(capsys, web=web_ok, env_file=ROOT / "coach_rules.md")
    assert code == 1 and "[提醒] 網頁檔案：缺少 app.js" in out


def test_busy_port_is_reminded(env, capsys, web_ok):
    env()
    code, out, _ = run(capsys, web=web_ok, port_busy=True, env_file=ROOT / "coach_rules.md")
    assert code == 1 and "[提醒] 8000 連接埠：已被占用" in out


def test_real_web_files_listed_by_check_all_exist():
    assert all((check.WEB / f).is_file() for f in check.WEB_FILES)


def test_port_probe_detects_a_listening_socket():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert check.port_in_use(s.getsockname()[1]) is True
    # 沒人聽的連接埠
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert check.port_in_use(free) is False


def test_llm_complete_forwards_check_options_only_when_given():
    from coach.llm import _OpenAICompatible

    class Resp:
        usage = None
        model = "m"
        choices = [type("C", (), {"message": type("M", (), {"content": "{}"})()})()]

    class Client:
        def __init__(self):
            self.kwargs = []
            self.chat = type("Chat", (), {"completions": self})()

        def create(self, **kw):
            self.kwargs.append(kw)
            return Resp()

    c = Client()
    llm = _OpenAICompatible(c)
    llm.model = "m"
    llm.complete([{"role": "user", "content": "x"}])
    llm.complete([{"role": "user", "content": "x"}], max_tokens=16, timeout=15)
    normal, ping = c.kwargs
    assert "max_completion_tokens" not in normal and "timeout" not in normal  # 正常流程不受影響
    assert ping["max_completion_tokens"] == 16 and ping["timeout"] == 15


# ---- .bat 與 VS Code 任務 ---------------------------------------------------


def _bat(name):
    raw = (ROOT / "bat" / name).read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")  # CRLF，cmd 才不會出怪事
    return raw.decode("utf-8")


def test_run_bat_has_required_commands():
    t = _bat("run.bat")
    assert 'cd /d "%~dp0.."' in t  # bat/ 的上一層才是專案根目錄
    assert ".venv\\Scripts\\activate.bat" in t and "找不到 .venv" in t
    commands = [ln for ln in t.splitlines() if not ln.lower().startswith(("rem ", "echo"))]
    assert any(ln.strip() == "python -m coach.launch" for ln in commands)  # 啟動改由 coach.launch 負責
    assert not any("--no-tunnel" in ln for ln in commands)
    assert t.rstrip().endswith("pause")


def test_check_bat_has_required_commands():
    t = _bat("check.bat")
    assert 'cd /d "%~dp0.."' in t  # bat/ 的上一層才是專案根目錄
    assert ".venv\\Scripts\\activate.bat" in t and "找不到 .venv" in t
    assert "python -m coach.check" in t
    assert t.count("pause") >= 2  # 找不到 .venv 與正常結束都會 pause，視窗不會一閃就關


def test_no_ps1_scripts_and_no_bat_left_in_root():
    assert not list(ROOT.glob("*.ps1")) and not list((ROOT / "bat").glob("*.ps1"))
    assert not list(ROOT.glob("*.bat"))  # 都放在 bat/


def test_vscode_tasks_define_both_tasks():
    data = json.loads((ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    tasks = {t["label"]: t for t in data["tasks"]}
    assert set(tasks) == {"管到飛：啟動", "管到飛：啟動（僅本機）", "管到飛：發表前檢查"}
    assert tasks["管到飛：啟動（僅本機）"]["command"].endswith("bat\\run_local.bat")
    assert tasks["管到飛：啟動"]["command"].endswith("bat\\run.bat")
    check_task = tasks["管到飛：發表前檢查"]
    assert check_task["args"] == ["-m", "coach.check"] and "python" in check_task["command"]


@pytest.mark.skipif(shutil.which("git") is None, reason="找不到 git")
def test_bat_files_and_tasks_are_not_gitignored():
    for path in ("bat/run.bat", "bat/run_local.bat", "bat/check.bat", ".vscode/tasks.json"):
        r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
        assert r.returncode == 1, f"{path} 被 .gitignore 排除了"
    # 其他 .vscode 檔案仍照舊排除
    assert subprocess.run(["git", "check-ignore", "-q", ".vscode/settings.json"], cwd=ROOT).returncode == 0
