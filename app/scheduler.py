"""进程内定时任务：过号自动标记、回港提醒、超时升级、停航广播。"""
from __future__ import annotations

import threading
import time

from .events import EventBus
from .service import Service


class Scheduler(threading.Thread):
    daemon = True

    def __init__(self, svc: Service, bus: EventBus, interval: float = 10.0):
        super().__init__(name="lake-scheduler")
        self.svc = svc
        self.bus = bus
        self.interval = interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                r = self.svc.scheduler_tick()
                if r["reminded"] or r["missed"]:
                    self.bus.publish("scheduler")
            except Exception as exc:  # 调度异常不应杀死线程
                print(f"[scheduler] {exc}")
