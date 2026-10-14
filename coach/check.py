"""發表前檢查：python -m coach.check

1. 讀 .env，對模型發一次最小請求，印出規則檔版本、部署名稱、延遲、成功或失敗（失敗給簡短中文提示）
2. 檢查網頁所需檔案、sky.jpg、logo.png、8000 連接埠
全部 OK 結束碼 0，否則 1。不會印出金鑰的任何字元。
"""

from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from coach.llm import LLMClient, get_llm
from coach import tunnel as tunnel_mod
from coach.rules import load_rules

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "coach" / "web"
PORT = 8000
PING_TIMEOUT = 15  # 秒
PING_MAX_TOKENS = 16

# 各供應商一定要有的環境變數（API 版本有預設值，不算必填）
REQUIRED_ENV = {
    "azure": ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"],
    "openai": ["OPENAI_API_KEY"],
}
SECRET_ENV = ["AZURE_OPENAI_API_KEY", "OPENAI_API_KEY"]

WEB_FILES = [
    "index.html",
    "style.css",
    "app.js",
    "intake.js",
    "export-image.js",
    "assets/icons/hat.svg",
    "assets/icons/gold.svg",
    "assets/icons/wand.svg",
    "assets/wings.svg",
]
OPTIONAL_ASSETS = ["assets/sky.jpg", "assets/logo.png"]  # 規格上要檢查；缺了頁面仍能用，但會提醒

PING_MESSAGES = [{"role": "user", "content": '請只回傳 JSON：{"ok": true}'}]


def scrub(text: str) -> str:
    """把環境變數裡的金鑰值從任何要印出的文字中遮掉。"""
    for name in SECRET_ENV:
        secret = os.environ.get(name, "").strip()
        if len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


def missing_env(provider: str) -> list[str]:
    return [n for n in REQUIRED_ENV.get(provider, []) if not os.environ.get(n, "").strip()]


def explain_error(e: Exception, provider: str = "azure") -> str:
    """把模型呼叫的錯誤翻成簡短中文原因與提示（不含金鑰）。"""
    name = type(e).__name__
    status = getattr(e, "status_code", None)
    key_var = "AZURE_OPENAI_API_KEY" if provider == "azure" else "OPENAI_API_KEY"
    if "Timeout" in name or isinstance(e, TimeoutError):
        return "逾時：模型太久沒回應。檢查網路，以及 AZURE_OPENAI_ENDPOINT 是否正確。"
    if "Connection" in name or isinstance(e, (ConnectionError, OSError)):
        return "連線失敗：連不到模型服務。檢查網路（公司或現場網路有時會擋），以及 AZURE_OPENAI_ENDPOINT 是否正確。"
    if status == 401:
        return f"金鑰錯誤（401）：檢查 .env 的 {key_var} 是否正確、是否過期，前後不要有多餘空白或引號。"
    if status == 404:
        return "找不到部署（404）：AZURE_OPENAI_DEPLOYMENT 要填 Azure 上的「部署名稱」，不是模型名稱；也檢查 AZURE_OPENAI_ENDPOINT。"
    if status == 400:
        return (
            "請求被拒絕（400）：最常見是 AZURE_OPENAI_API_VERSION 填錯。"
            "要填的是 API 版本（例如 2024-10-21），不是模型版本。"
        )
    if status == 403:
        return "沒有權限（403）：這把金鑰或資源沒有使用該部署的權限。"
    if status == 429:
        return "被限流（429）：請求太頻繁或額度用完，稍後再試。"
    if status:
        return f"伺服器回應錯誤（{status}）。"
    return f"無法完成請求（{name}）。"


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _line(ok: bool, label: str, detail: str = "") -> bool:
    print(f"[{'OK' if ok else '提醒'}] {label}" + (f"：{detail}" if detail else ""))
    return ok


def check_model(llm_factory: Callable[[], LLMClient], provider: str) -> bool:
    missing = missing_env(provider)
    if missing:
        return _line(False, "模型連線", f".env 缺少欄位：{'、'.join(missing)}（參考 .env.example）")
    try:
        llm = llm_factory()
    except Exception as e:  # noqa: BLE001 - 建立 client 失敗，一律回報
        return _line(False, "模型連線", scrub(str(e)))
    print(f"      模型部署：{llm.model}（{llm.provider}）")
    start = time.perf_counter()
    try:
        llm.complete(PING_MESSAGES, max_tokens=PING_MAX_TOKENS, timeout=PING_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        ms = int((time.perf_counter() - start) * 1000)
        _line(False, "模型連線", f"失敗（{ms} ms）")
        print(f"      原因：{explain_error(e, provider)}")
        detail = scrub(str(e)).strip().replace("\n", " ")
        if detail:
            print(f"      原始訊息：{detail[:200]}")
        return False
    ms = int((time.perf_counter() - start) * 1000)
    return _line(True, "模型連線", f"成功，延遲 {ms} ms")


def check_files(web: Path = WEB) -> bool:
    ok = True
    missing = [f for f in WEB_FILES if not (web / f).is_file()]
    ok &= _line(not missing, "網頁檔案", "齊全" if not missing else f"缺少 {'、'.join(missing)}")
    for f in OPTIONAL_ASSETS:
        exists = (web / f).is_file()
        ok &= _line(exists, f"coach/web/{f}", "存在" if exists else "不存在（頁面仍可使用，但不會有這張圖）")
    return ok


def check_port(probe: Callable[[int], bool], port: int = PORT) -> bool:
    busy = probe(port)
    return _line(not busy, f"{port} 連接埠", "可使用" if not busy else "已被占用（可能已經有一個在跑，先關掉再啟動）")


def qrcode_ok() -> bool:
    try:
        return "<svg" in tunnel_mod.make_qr_svg("https://example.com/")
    except Exception:  # noqa: BLE001 - 沒安裝或壞掉都算不可用
        return False


def check_outbound(
    port_busy: bool,
    ngrok_version_fn: Callable[[], str | None],
    qrcode_probe: Callable[[], bool],
    tunnel_factory: Callable[[], tunnel_mod.Tunnel],
) -> bool:
    """對外功能：ngrok 是否安裝、qrcode 套件、對外通道實測。失敗只標「提醒」，由結束碼反映。"""
    print("\n對外功能")
    version = ngrok_version_fn()
    ngrok_ok = _line(version is not None, "ngrok", version or tunnel_mod.explain_reason("not_found"))
    qr_available = qrcode_probe()
    qr_ok = _line(qr_available, "qrcode 套件", "可用" if qr_available else "不可用（執行 pip install -r requirements.txt）")

    if port_busy:
        _line(True, "對外通道實測", "略過：8000 已被占用，表示伺服器正在跑（不會另開通道干擾它）")
        return ngrok_ok and qr_ok
    if not ngrok_ok:
        _line(True, "對外通道實測", "略過：沒有 ngrok（上面已提醒）")
        return False

    tunnel = tunnel_factory()
    try:
        result = tunnel.start()
    except Exception as e:  # noqa: BLE001
        result = tunnel_mod.TunnelResult(None, "exited")
        print(f"      （通道啟動時發生錯誤：{type(e).__name__}）")
    finally:
        try:
            tunnel.stop()
        except Exception:  # noqa: BLE001
            pass
    leftover = tunnel.is_running()
    if result.url:
        _line(True, "對外通道實測", f"取得公開網址 {result.url}")
        tunnel_ok = _line(not leftover, "通道已關閉", "沒有殘留程序" if not leftover else "ngrok 還在執行，請到工作管理員結束 ngrok")
    else:
        tunnel_ok = _line(False, "對外通道實測", "取不到公開網址")
        print(f"      原因：{tunnel_mod.explain_reason(result.reason or '')}")
        if leftover:
            _line(False, "通道已關閉", "ngrok 還在執行，請到工作管理員結束 ngrok")
            tunnel_ok = False
    return ngrok_ok and qr_ok and tunnel_ok


def main(
    argv: list[str] | None = None,
    llm_factory: Callable[[], LLMClient] = get_llm,
    port_probe: Callable[[int], bool] = port_in_use,
    env_file: Path = ROOT / ".env",
    web: Path = WEB,
    ngrok_version_fn: Callable[[], str | None] = tunnel_mod.ngrok_version,
    qrcode_probe: Callable[[], bool] = qrcode_ok,
    tunnel_factory: Callable[[], tunnel_mod.Tunnel] | None = None,
) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    env_exists = env_file.is_file()
    load_dotenv(env_file)
    provider = os.environ.get("LLM_PROVIDER", "azure").strip().lower() or "azure"

    print("管到飛 發表前檢查")
    try:
        rules = load_rules()
        _line(True, "規則檔", f"{rules.version}（{rules.version_date}）sha {rules.sha256[:8]}")
        rules_ok = True
    except Exception as e:  # noqa: BLE001
        rules_ok = _line(False, "規則檔", f"讀取失敗：{type(e).__name__}")
    if not env_exists:
        _line(False, ".env", "找不到 .env（把 .env.example 複製成 .env 再填值）")
    if provider not in REQUIRED_ENV:
        model_ok = _line(False, "模型連線", f"LLM_PROVIDER 不認得：{provider}（可用：{'、'.join(REQUIRED_ENV)}）")
    else:
        model_ok = check_model(llm_factory, provider)
    files_ok = check_files(web)
    port_ok = check_port(port_probe)
    port_busy = not port_ok
    factory = tunnel_factory or (lambda: tunnel_mod.NgrokTunnel(PORT, log_path=ROOT / "outputs" / "ngrok_check.log"))
    outbound_ok = check_outbound(port_busy, ngrok_version_fn, qrcode_probe, factory)

    all_ok = rules_ok and env_exists and model_ok and files_ok and port_ok and outbound_ok
    print("\n全部 OK，可以發表。" if all_ok else "\n有項目需要處理，請看上面標示「提醒」的地方。")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
