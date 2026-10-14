"""對外公開網址：ngrok 通道、QR code、網址檔。

純函式（pick_https_url、classify_log、make_qr_svg、write_public_url、clear_qr_dir）與可替換的元件
（NgrokTunnel 的 which / popen / fetch / clock / sleep 都能換成替身），測試不需要連網。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

ROOT = Path(__file__).resolve().parent.parent
QR_DIR = ROOT / "outputs" / "qr"
URL_FILE = "public_url.txt"
QR_FILE = "qr.svg"
NGROK_ADMIN_PORTS = (4040, 4041, 4042)
WAIT_SECONDS = 15


# ---- 純函式 ---------------------------------------------------------------


def pick_https_url(payload, port: int = 8000) -> str | None:
    """從 ngrok 管理介面 /api/tunnels 的回應挑出轉到本機 port 的 https 公開網址；沒有就 None。"""
    if not isinstance(payload, dict):
        return None
    tunnels = payload.get("tunnels")
    if not isinstance(tunnels, list):
        return None
    for t in tunnels:
        if not isinstance(t, dict):
            continue
        url = t.get("public_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        addr = (t.get("config") or {}).get("addr") if isinstance(t.get("config"), dict) else None
        # 其他程式開的通道（管理介面 4040 可能是別人的）不能拿來用；沒有 addr 資訊就不挑剔
        if isinstance(addr, str) and not re.search(rf"(:|//|^){port}$", addr.rstrip("/")):
            continue
        return url
    return None


# 失敗代碼：not_found 找不到 ngrok｜auth authtoken 未設定或無效｜network 連線被擋｜timeout 逾時｜exited 程序提早結束
REASONS = {
    "not_found": ("找不到 ngrok", "請先安裝 ngrok 並加入 PATH（終端機輸入 ngrok version 應該有回應）。"),
    "auth": ("ngrok 驗證失敗", "authtoken 未設定或無效：到 ngrok 後台複製 authtoken，執行 ngrok config add-authtoken <你的 token>。"),
    "network": ("連不到 ngrok 服務", "網路被擋或不通，建議改用手機熱點，或先用 bat\\run_local.bat 只在本機執行。"),
    "timeout": ("等不到公開網址（逾時）", "網路可能太慢或被擋，建議改用手機熱點；也可以先用 bat\\run_local.bat 只在本機執行。"),
    "exited": ("ngrok 啟動後馬上結束了", "請在終端機手動執行 ngrok http 8000 看它的錯誤訊息。"),
}


def explain_reason(code: str) -> str:
    reason, hint = REASONS.get(code, ("取不到公開網址", "請看 ngrok 的訊息。"))
    return f"{reason}。{hint}"


def classify_log(log: str) -> str:
    """從 ngrok 的記錄判斷失敗原因代碼。"""
    low = log.lower()
    if "authtoken" in low or "authentication failed" in low or "err_ngrok_105" in low or "err_ngrok_4018" in low:
        return "auth"
    if any(s in low for s in ("dial tcp", "no such host", "connection refused", "i/o timeout", "tls", "failed to connect", "network is unreachable", "proxyconnect")):
        return "network"
    return "exited"


def make_qr_svg(url: str, size: str | None = "100%") -> str:
    """產生 QR code 的 SVG 字串（qrcode 套件，不需要 Pillow）。size 設 None 就保留原本的 mm 寬高。"""
    import qrcode
    from qrcode.image.svg import SvgPathImage

    svg = qrcode.make(url, image_factory=SvgPathImage, border=4).to_string().decode("utf-8")
    svg = re.sub(r"<\?xml[^>]*\?>\s*", "", svg)
    if size:
        svg = re.sub(r'<svg width="[^"]*" height="[^"]*"', f'<svg width="{size}" height="{size}"', svg, count=1)
    return svg


def clear_qr_dir(qr_dir: Path = QR_DIR) -> None:
    """啟動前清掉上一次的網址檔與 QR 圖檔，不留舊資料。"""
    shutil.rmtree(qr_dir, ignore_errors=True)


def write_public_url(url: str, qr_dir: Path = QR_DIR) -> None:
    """寫入 public_url.txt 與 qr.svg；每次都直接覆蓋。"""
    qr_dir.mkdir(parents=True, exist_ok=True)
    (qr_dir / URL_FILE).write_text(url + "\n", encoding="utf-8")
    (qr_dir / QR_FILE).write_text(make_qr_svg(url, size=None), encoding="utf-8")


def read_public_url(qr_dir: Path = QR_DIR, env: dict | None = None) -> str | None:
    """最新的公開網址：先讀 public_url.txt，沒有檔案才用環境變數 PUBLIC_URL，都沒有就 None。"""
    import os

    path = qr_dir / URL_FILE
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return text
    value = ((env if env is not None else os.environ).get("PUBLIC_URL") or "").strip()
    return value or None


# ---- ngrok 通道 -----------------------------------------------------------


def http_fetch(port: int, timeout: float = 1.0):
    """讀 ngrok 本機管理介面；連不上回 None。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tunnels", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 還沒起來、被占用或回應不是 JSON，都當作「還沒有」
        return None


@dataclass
class TunnelResult:
    url: str | None
    reason: str | None = None  # 失敗代碼（見 REASONS）


class Tunnel(Protocol):
    def start(self) -> TunnelResult: ...

    def stop(self) -> None: ...

    def is_running(self) -> bool: ...


class NgrokTunnel:
    def __init__(
        self,
        port: int = 8000,
        *,
        which: Callable[[str], str | None] = shutil.which,
        popen=subprocess.Popen,
        fetch: Callable[[int], object] = http_fetch,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        log_path: Path | None = None,
        timeout: float = WAIT_SECONDS,
        admin_ports: tuple[int, ...] = NGROK_ADMIN_PORTS,
    ) -> None:
        self.port = port
        self._which, self._popen, self._fetch = which, popen, fetch
        self._clock, self._sleep = clock, sleep
        self.log_path = log_path
        self.timeout = timeout
        self.admin_ports = admin_ports
        self.process = None
        self._log_file = None

    def _read_log(self) -> str:
        if not self.log_path:
            return ""
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def start(self) -> TunnelResult:
        exe = self._which("ngrok")
        if not exe:
            return TunnelResult(None, "not_found")
        # 輸出導到記錄檔，不佔用終端機畫面（ngrok 預設是全螢幕介面）
        out = subprocess.DEVNULL
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(self.log_path, "wb")  # noqa: SIM115 - stop() 時才關
            out = self._log_file
        try:
            self.process = self._popen(
                [exe, "http", str(self.port), "--log=stdout"], stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT
            )
        except OSError:
            self._close_log()
            return TunnelResult(None, "not_found")
        deadline = self._clock() + self.timeout
        while True:
            if self.process.poll() is not None:  # 程序已經結束：不用等到逾時，直接看記錄判斷原因
                return TunnelResult(None, classify_log(self._read_log()))
            for admin in self.admin_ports:
                url = pick_https_url(self._fetch(admin), self.port)
                if url:
                    return TunnelResult(url)
            if self._clock() >= deadline:
                code = classify_log(self._read_log())
                return TunnelResult(None, code if code in ("auth", "network") else "timeout")
            self._sleep(0.5)

    def _close_log(self) -> None:
        if self._log_file:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        """確實結束 ngrok：先請它結束，等不到就強制結束。"""
        p = self.process
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001 - 逾時或其他：強制結束
                p.kill()
                try:
                    p.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        self._close_log()


def ngrok_version(run=subprocess.run, which: Callable[[str], str | None] = shutil.which) -> str | None:
    """已安裝回版本字串，否則 None。"""
    exe = which("ngrok")
    if not exe:
        return None
    try:
        r = run([exe, "version"], capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return None
    return (r.stdout or "").strip() or None if r.returncode == 0 else None
