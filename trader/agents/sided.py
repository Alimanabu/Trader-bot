"""Стратегии для фьючерсного демо-режима: ставка на падение и на обе стороны.

Берём стратегию на правилах и переводим её сигналы:
- «медведь» (short): бычий сигнал базовой стратегии = выйти в деньги, медвежий = шорт;
- «двусторонний» (both): бычий = лонг, медвежий = шорт, «держать» = не трогать позицию.
Подходят только семейства, у которых сигнал SELL означает «рынок медвежий», а не «нет сигнала».
"""
from __future__ import annotations

from ..models import Action, Candle, Signal
from .base import Strategy

SHORTABLE = ["sma_cross", "ema_momentum", "rsi_reversion", "bollinger", "breakout", "macd", "zscore",
             "rsi_divergence", "supertrend", "keltner", "mtf",
             "bb_rsi", "ichimoku", "ema_ribbon", "adx_dmi", "squeeze", "vwap_breakout", "stoch_rsi", "cmf_trend"]


class Sided(Strategy):
    def __init__(self, base: Strategy, side: str):
        self.base = base
        self.side = side
        self.family = f"{base.family}_{side}"
        self.description = ("Медведь: " if side == "short" else "Двусторонний: ") + base.description
        self.params = base.params
        self.timeframe = getattr(base, "timeframe", "1h")
        self.high_frequency = getattr(base, "high_frequency", False)

    @classmethod
    def default_params(cls):
        return {}

    def warmup(self):
        return self.base.warmup()

    def cadence_minutes(self):
        return self.base.cadence_minutes()

    def uses_llm(self):
        return self.base.uses_llm()

    def decide(self, candles: list[Candle], context: dict | None = None) -> Signal:
        ctx = dict(context or {})
        signed = float(ctx.get("exposure", 0.0))
        ctx["exposure"] = max(0.0, signed)             # базовая стратегия думает только о лонге
        sig = self.base.decide(candles, ctx)
        if sig.action == Action.BUY:
            target = 0.0 if self.side == "short" else abs(sig.target_exposure)
            action = Action.SELL if self.side == "short" and signed < 0 else (Action.BUY if target > 0 else Action.HOLD)
            reason = (("закрываю шорт: " if signed < 0 else "вне рынка, сигнал бычий: ") if self.side == "short" else "лонг: ") + sig.reason
        elif sig.action == Action.SELL:
            target = -1.0
            action = Action.SELL
            reason = "шорт: " + sig.reason
        else:
            target = signed
            action = Action.HOLD
            reason = sig.reason
        return Signal(action, target, sig.confidence, reason, dict(sig.meta))
