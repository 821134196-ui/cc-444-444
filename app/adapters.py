"""模拟适配器:天气评估与支付渠道。均为进程内模拟,不访问外部服务。"""
from __future__ import annotations

import random
import time

# 天气状态:fine 适航 / windy 大风预警(仍可出航但提示) / storm 停航
WEATHER_MESSAGES = {
    "fine": "湖面风浪 1 级,适航",
    "windy": "风力 5 级,注意安全,谨慎出航",
    "storm": "雷暴大风,已发布停航令",
}


class WeatherAdapter:
    """模拟气象数据源。

    - evaluate(): 按概率给出新的天气评估结果(大部分时间保持适航)。
    - force(): 人工/演练强制切换天气(用于"天气突变"处理与页面演示)。
    """

    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.Random()

    def evaluate(self, current: str) -> tuple[str, str]:
        roll = self._rng.random()
        if current == "storm":
            # 停航后大概率持续,小概率恢复
            new = "fine" if roll < 0.35 else "storm"
        elif current == "windy":
            new = "fine" if roll < 0.5 else ("storm" if roll < 0.7 else "windy")
        else:
            new = "fine" if roll < 0.8 else ("windy" if roll < 0.95 else "storm")
        return new, WEATHER_MESSAGES[new]

    def force(self, status: str) -> tuple[str, str]:
        return status, WEATHER_MESSAGES[status]


class PaymentAdapter:
    """模拟支付渠道:票款、押金预授权、押金扣除、押金退还。

    幂等由调用方用 idem_key 保证;这里对同一 idem_key 返回稳定的渠道流水。
    """

    def __init__(self):
        self._refs: dict[str, str] = {}
        self._seq = 0

    def _ref(self, idem_key: str) -> str:
        if idem_key not in self._refs:
            self._seq += 1
            self._refs[idem_key] = f"PAY-{int(time.time())}-{self._seq:05d}"
        return self._refs[idem_key]

    def charge(self, amount: float, idem_key: str, kind: str) -> dict:
        """扣款(票款 / 押金扣除)。"""
        return {
            "ok": True,
            "provider_ref": self._ref(idem_key),
            "amount": round(amount, 2),
            "kind": kind,
        }

    def hold(self, amount: float, idem_key: str) -> dict:
        """押金预授权(演示,不真实冻结)。"""
        return {
            "ok": True,
            "provider_ref": self._ref(idem_key),
            "amount": round(amount, 2),
            "kind": "hold",
        }

    def refund(self, amount: float, idem_key: str) -> dict:
        return {
            "ok": True,
            "provider_ref": self._ref(idem_key),
            "amount": round(amount, 2),
            "kind": "refund",
        }
