"""进程内发布订阅：状态变更后推送 SSE。"""
from __future__ import annotations

import threading
import time


class EventBus:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._subs: set[int] = set()
        self._seq = 0

    def subscribe(self) -> int:
        with self._cond:
            self._seq += 1
            self._subs.add(self._seq)
            return self._seq

    def unsubscribe(self, sid: int) -> None:
        with self._cond:
            self._subs.discard(sid)

    def publish(self, event: str = "update") -> None:
        with self._cond:
            self._cond.notify_all()
        self._last = (time.time(), event)

    def wait(self, timeout: float = 25.0) -> bool:
        with self._cond:
            return self._cond.wait(timeout)
