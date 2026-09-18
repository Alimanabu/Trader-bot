"""Экспериментальный скальпер: торгует на минутных свечах, много и быстро.

Идея: возврат к среднему на минутках. Цена ушла ниже полосы (z-score ниже порога) → покупка,
выход при возврате к среднему, но только если прибыль покрывает комиссии за круг, иначе ждём
дальше или выходим по стопу. Живёт вне десков, на своём счёте, освобождён от лимита входов.
Нужен, чтобы на живых данных проверить, окупается ли частая торговля при комиссии 0,1%.
"""
from __future__ import annotations

from ..data import indicators as ind
from ..models import Action, Candle, Signal
from .base import Strategy, hold


class Scalper(Strategy):
    family = "scalper"
    description = "Скальпер (1 мин): покупает провал ниже минутной полосы, выходит на возврате к среднему, если прибыль покрывает комиссии."
    side = "long"
    timeframe = "1m"
    high_frequency = True

    @classmethod
    def default_params(cls) -> dict:
        return {"period": 30, "entry_z": -2.0, "exit_z": 0.0, "min_gain_pct": 0.3, "give_up_z": 1.5}

    @classmethod
    def param_grid(cls) -> dict[str, list]:
        return {"period": [20, 30, 60], "entry_z": [-1.5, -2.0, -2.5], "min_gain_pct": [0.25, 0.3, 0.4]}

    def warmup(self) -> int:
        return int(self.params["period"]) + 5

    def cadence_minutes(self) -> int:
        return 1

    def decide(self, candles: list[Candle], context: dict | None = None) -> Signal:
        ctx = context or {}
        exposure = float(ctx.get("exposure", 0.0))
        n = int(self.params["period"])
        closes = [c.close for c in candles]
        if len(closes) < self.warmup():
            return hold("скальпер: набираю минутные свечи", exposure)
        window = closes[-n:]
        mean = sum(window) / n
        var = sum((x - mean) ** 2 for x in window) / n
        sd = var ** 0.5
        price = closes[-1]
        z = (price - mean) / sd if sd > 0 else 0.0
        if exposure <= 1e-9:
            if z <= float(self.params["entry_z"]):
                return Signal(Action.BUY, 1.0, min(1.0, abs(z) / 3), f"скальп: z={z:.2f} ниже {self.params['entry_z']}, среднее {mean:.0f}")
            return hold(f"скальп: z={z:.2f}, жду провал ниже {self.params['entry_z']}", 0.0)
        entry = float(ctx.get("entry") or 0.0)
        gain = (price / entry - 1) * 100 if entry else 0.0
        if z >= float(self.params["exit_z"]) and gain >= float(self.params["min_gain_pct"]):
            return Signal(Action.SELL, 0.0, 0.8, f"скальп: возврат к среднему, прибыль {gain:+.2f}% покрывает комиссии")
        if z >= float(self.params["give_up_z"]):
            return Signal(Action.SELL, 0.0, 0.5, f"скальп: z={z:.2f}, выше среднего, выхожу ({gain:+.2f}%)")
        return hold(f"скальп: в позиции, z={z:.2f}, прибыль {gain:+.2f}%, жду {self.params['min_gain_pct']}%", exposure)


EXPERIMENT_STRATEGIES: list[type[Strategy]] = [Scalper]
