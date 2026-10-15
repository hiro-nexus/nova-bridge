"""Siri ショートカット（Webhook）経由の投稿のための、流量制限と確認待ちの管理。Slack には触れない。"""
import threading
import time
from collections import deque


class RateLimiter:
    """window 秒のあいだに limit 件まで許可する。超えたら断り、知らせるのは window ごとに1度だけ。"""

    def __init__(self, limit: int = 20, window: float = 600.0):
        self.limit = limit
        self.window = window
        self.events: deque[float] = deque()
        self.last_notified = float("-inf")
        self.lock = threading.Lock()

    def allow(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.lock:
            while self.events and now - self.events[0] >= self.window:
                self.events.popleft()
            if len(self.events) >= self.limit:
                return False
            self.events.append(now)
            return True

    def should_notify(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.lock:
            if now - self.last_notified >= self.window:
                self.last_notified = now
                return True
            return False


class PendingConfirmations:
    """Hiroさん本人の確認を待っている音声の指示。再起動すると消える（安全側）。"""

    def __init__(self, ttl: float = 24 * 3600):
        self.ttl = ttl
        self.items: dict[str, tuple[float, str]] = {}
        self.lock = threading.Lock()

    def add(self, ts: str, text: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.lock:
            for key in [k for k, (added, _) in self.items.items() if now - added > self.ttl]:
                del self.items[key]  # 期限切れを掃除する
            self.items[ts] = (now, text)

    def pop(self, ts: str, now: float | None = None) -> tuple[str, str]:
        """(状態, 本文) を返す。状態は ok / expired / none。取り出したら消える。"""
        now = time.time() if now is None else now
        with self.lock:
            entry = self.items.pop(ts, None)
        if entry is None:
            return "none", ""
        added, text = entry
        if now - added > self.ttl:
            return "expired", ""
        return "ok", text
