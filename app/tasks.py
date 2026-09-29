"""进程内后台任务:
- 每 20 秒扫描超时未归批次 -> 提醒 + 广播(每批次只提醒一次)
- 每 60 秒让模拟天气适配器评估一次,天气突变(进入/解除停航)自动广播
- 每 10 秒清理叫号超时 -> 过号
"""
from __future__ import annotations

import threading
import time

from . import services
from .adapters import WeatherAdapter


class BackgroundTasks:
    def __init__(self, weather: WeatherAdapter | None = None,
                 tick_interval: int = 10):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._weather = weather or services.weather_adapter
        self.tick_interval = tick_interval
        self._last_weather = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="lake-bg-tasks")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                services.expire_called()
                services.check_overdue()
                now = time.time()
                if now - self._last_weather >= 60:
                    self._last_weather = now
                    cur = services.current_weather()["status"]
                    new, _msg = self._weather.evaluate(cur)
                    if new != cur:
                        services.set_weather(new, source="气象模拟")
            except Exception as exc:  # 后台任务永不致死进程
                print(f"[bg] {exc}")
            self._stop.wait(self.tick_interval)
