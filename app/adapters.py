"""模拟适配器：天气与支付。

- WeatherAdapter：生成天气，可被测试/演示脚本强制设定；WARNING 提示，SUSPEND 停航。
- PaymentAdapter：模拟付款结果，按幂等键返回同一笔流水，重复提交不重复扣款。
"""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass


@dataclass
class Weather:
    level: str          # NORMAL / WARNING / SUSPEND
    condition: str
    wind: int           # 风速 km/h
    message: str

    def as_dict(self) -> dict:
        return {"level": self.level, "condition": self.condition,
                "wind": self.wind, "message": self.message}


# 风级 -> 运营决策
_CONDITIONS = [
    ("NORMAL", "晴", 8, "湖面微风，正常运营"),
    ("NORMAL", "多云", 14, "天气适宜，正常运营"),
    ("WARNING", "阵风加大", 32, "风力增强，登船请抓紧扶手"),
    ("SUSPEND", "雷阵雨", 58, "强对流天气，全面停航"),
    ("SUSPEND", "大风预警", 66, "大风预警，所有船只回港避风"),
]


class WeatherAdapter:
    """默认随机轮换样例天气；测试可 force() 锁定。"""

    def __init__(self) -> None:
        self._forced: Weather | None = None
        self._cycle = itertools.cycle(_CONDITIONS)
        self._lock = threading.Lock()

    def force(self, level: str | None) -> None:
        with self._lock:
            if level is None:
                self._forced = None
            else:
                row = next(c for c in _CONDITIONS if c[0] == level)
                self._forced = Weather(*row)

    def current(self) -> Weather:
        with self._lock:
            if self._forced is not None:
                return self._forced
            return Weather(*next(self._cycle))

    def is_suspended(self) -> bool:
        return self.current().level == "SUSPEND"


class PaymentAdapter:
    """模拟支付渠道。幂等键相同则返回同一结果，绝不重复扣款。"""

    def __init__(self, fail_keys: set[str] | None = None) -> None:
        self._seen: dict[str, dict] = {}
        self._fail_keys = fail_keys or set()
        self._seq = itertools.count(10001)
        self._lock = threading.Lock()

    def charge(self, *, idempotency_key: str, amount: int, kind: str) -> dict:
        with self._lock:
            if idempotency_key in self._seen:
                return {**self._seen[idempotency_key], "replayed": True}
            ok = idempotency_key not in self._fail_keys
            result = {
                "reference": f"PAY-{next(self._seq)}",
                "status": "SUCCESS" if ok else "FAILED",
                "amount": amount,
                "kind": kind,
                "replayed": False,
            }
            self._seen[idempotency_key] = result
            return result
