"""簡單的記憶體限流（公開之後防止模型額度被耗盡）。重新啟動就歸零，不需要資料庫。

預設：每個來源 IP 每 10 分鐘最多 30 次；全站每小時最多 300 次。可用環境變數調整：
  RATE_LIMIT_IP_MAX、RATE_LIMIT_IP_WINDOW_SECONDS、RATE_LIMIT_GLOBAL_MAX、RATE_LIMIT_GLOBAL_WINDOW_SECONDS
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Callable, Mapping

from coach.flow import FlowError

TOO_MANY_MESSAGE = "目前使用的人比較多，請稍後再試"


class TooManyRequests(FlowError):
    status_code = 429


def client_ip(headers: Mapping[str, str], peer: str | None, trusted_hops: int | None = None) -> str:
    """來源 IP。

    trusted_hops=None（本機／ngrok）：CF-Connecting-IP → X-Forwarded-For 的第一個值 → 連線位址。
    ngrok 這類通道會讓所有連線看起來都來自本機，所以不能只看連線位址。

    trusted_hops=N（雲端，N≥1）：前面有 N 層代理，每層都把「它看到的來源」附加到 X-Forwarded-For 尾端。
    用戶端可以自己送假的 X-Forwarded-For（會排在最前面），所以只信右邊算起第 N 個，並且忽略 CF-Connecting-IP。
    標頭層數不夠（有人繞過代理直連）就用連線位址。
    """
    lower = {k.lower(): v for k, v in headers.items()}
    xff = [p.strip() for p in (lower.get("x-forwarded-for") or "").split(",") if p.strip()]
    if trusted_hops:
        return xff[-trusted_hops] if len(xff) >= trusted_hops else (peer or "unknown")
    cf = (lower.get("cf-connecting-ip") or "").strip()
    if cf:
        return cf
    if xff:
        return xff[0]
    return peer or "unknown"


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(env.get(name, "").strip())
        return value if value > 0 else default
    except ValueError:
        return default


class RateLimiter:
    def __init__(
        self,
        ip_max: int = 30,
        ip_window: float = 600,
        global_max: int = 300,
        global_window: float = 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ip_max, self.ip_window = ip_max, ip_window
        self.global_max, self.global_window = global_max, global_window
        self._clock = clock
        self._ip: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RateLimiter":
        env = os.environ if env is None else env
        return cls(
            ip_max=_env_int(env, "RATE_LIMIT_IP_MAX", 30),
            ip_window=_env_int(env, "RATE_LIMIT_IP_WINDOW_SECONDS", 600),
            global_max=_env_int(env, "RATE_LIMIT_GLOBAL_MAX", 300),
            global_window=_env_int(env, "RATE_LIMIT_GLOBAL_WINDOW_SECONDS", 3600),
        )

    @staticmethod
    def _trim(q: deque[float], now: float, window: float) -> None:
        while q and now - q[0] >= window:
            q.popleft()

    def allow(self, ip: str) -> bool:
        """允許就記一次並回 True；超過限制回 False（被擋的請求不計入）。"""
        with self._lock:
            now = self._clock()
            self._trim(self._global, now, self.global_window)
            q = self._ip.setdefault(ip, deque())
            self._trim(q, now, self.ip_window)
            if len(q) >= self.ip_max or len(self._global) >= self.global_max:
                return False
            q.append(now)
            self._global.append(now)
            if len(self._ip) > 10000:  # 避免記憶體無限成長：丟掉已經過期的 IP
                for key in [k for k, v in self._ip.items() if not v or now - v[-1] >= self.ip_window]:
                    del self._ip[key]
            return True

    def check(self, headers: Mapping[str, str], peer: str | None, trusted_hops: int | None = None) -> None:
        if not self.allow(client_ip(headers, peer, trusted_hops)):
            raise TooManyRequests(TOO_MANY_MESSAGE)
