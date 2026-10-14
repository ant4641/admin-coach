"""發表用啟動程式：python -m coach.launch [--no-tunnel]

清舊檔 → 檢查 8000 → 啟動 uvicorn → （可用時）啟動 ngrok 取得公開網址 → 寫網址檔與 QR → 開瀏覽器。
取不到公開網址不會中止，伺服器照常運作。結束時（Ctrl-C、關視窗）確實結束 uvicorn 與 ngrok 兩個子程序。
"""

from __future__ import annotations

import argparse
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from coach import tunnel as tunnel_mod
from coach.tunnel import QR_DIR, ROOT, Tunnel, clear_qr_dir, explain_reason, write_public_url

PORT = 8000
HOME_URL = f"http://127.0.0.1:{PORT}/"
QR_URL = f"http://127.0.0.1:{PORT}/qr"
NGROK_LOG = ROOT / "outputs" / "ngrok.log"
START_TIMEOUT = 30  # 秒


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def spawn_server() -> subprocess.Popen:
    # 不用 --reload：現場檔案變動時不要自動重啟
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "coach.api:app", "--host", "127.0.0.1", "--port", str(PORT)], cwd=ROOT
    )


def wait_for_port(port: int, server, timeout: float = START_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            return False
        if port_in_use(port):
            return True
        time.sleep(0.3)
    return False


def wait_until_exit(server) -> None:
    """阻塞到伺服器結束；Ctrl-C 會丟 KeyboardInterrupt 出去，由 run() 的 finally 收尾。"""
    while server.poll() is None:
        time.sleep(0.5)


def stop_process(p, timeout: float = 5) -> None:
    """結束子程序：先請它結束，等不到就強制結束。"""
    if p is None or p.poll() is not None:
        return
    p.terminate()
    try:
        p.wait(timeout=timeout)
    except Exception:  # noqa: BLE001
        p.kill()
        try:
            p.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass


@dataclass
class Deps:
    """可替換的元件；測試時換成替身，不啟動真的程序、不連網。"""

    port_in_use: Callable[[int], bool] = port_in_use
    spawn_server: Callable[[], object] = spawn_server
    wait_for_port: Callable[[int, object], bool] = wait_for_port
    tunnel_factory: Callable[[], Tunnel] = lambda: tunnel_mod.NgrokTunnel(PORT, log_path=NGROK_LOG)
    open_url: Callable[[str], object] = webbrowser.open_new_tab
    wait: Callable[[object], None] = wait_until_exit
    qr_dir: Path = QR_DIR
    log: Callable[[str], None] = print
    opened: list = field(default_factory=list)


def run(no_tunnel: bool = False, deps: Deps | None = None) -> int:
    d = deps or Deps()
    # 1. 清掉上一次的網址檔與 QR 圖檔
    clear_qr_dir(d.qr_dir)

    # 2. 連接埠被占用：不啟動
    if d.port_in_use(PORT):
        d.log(f"[提醒] {PORT} 連接埠已被占用。請先關閉舊的視窗（上一次的管到飛），再重新啟動。")
        return 1

    server = None
    tunnel = None
    try:
        # 3. 啟動伺服器，等到連得上
        server = d.spawn_server()
        if not d.wait_for_port(PORT, server):
            d.log("[提醒] 伺服器沒有成功啟動，請看上面的錯誤訊息。")
            return 1
        d.log(f"伺服器已啟動：{HOME_URL}")

        # 4～5. 公開網址（失敗不中止）
        public_url = None
        if no_tunnel:
            d.log("只在本機執行（--no-tunnel），不啟動對外通道。")
        else:
            try:
                tunnel = d.tunnel_factory()
                result = tunnel.start()
            except Exception as e:  # noqa: BLE001 - 通道的任何問題都不能讓伺服器停掉
                result = tunnel_mod.TunnelResult(None, "exited")
                d.log(f"（通道啟動時發生錯誤：{type(e).__name__}）")
            if result.url:
                public_url = result.url
                write_public_url(public_url, d.qr_dir)
                d.log(f"公開網址：{public_url}")
            else:
                d.log(f"[提醒] 目前沒有公開網址：{explain_reason(result.reason or '')}")
                d.log("伺服器仍照常運作，可以先在本機使用。")

        # 6～7. 開瀏覽器：首頁一定開；取得公開網址才再開 /qr
        d.open_url(HOME_URL)
        if public_url:
            d.open_url(QR_URL)
        d.log("要結束請按 Ctrl+C 或關閉這個視窗。")

        d.wait(server)
        return 0
    except KeyboardInterrupt:
        d.log("\n正在結束…")
        return 0
    finally:
        # 9. 兩個子程序都要結束，不留殘留程序
        if tunnel is not None:
            try:
                tunnel.stop()
            except Exception:  # noqa: BLE001
                pass
        stop_process(server)


def _install_signal_handlers() -> None:
    """關視窗、被終止時也走 finally 收尾，而不是直接被砍而留下子程序。"""

    def handler(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    parser = argparse.ArgumentParser(description="管到飛：啟動伺服器與對外通道")
    parser.add_argument("--no-tunnel", action="store_true", help="只在本機執行，不啟動 ngrok")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if deps is None:
        _install_signal_handlers()
    return run(no_tunnel=args.no_tunnel, deps=deps)


if __name__ == "__main__":
    sys.exit(main())
