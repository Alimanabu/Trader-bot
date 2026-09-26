"""Последователь внешних сигналов: торгует не по индикаторам, а по сигналам проверенного источника.

Источник (телеграм-канал, трейдер с биржи, знакомый) сначала проходит проверку в отделе внешних
сигналов на бумаге. Только когда точность подтверждена, для него создаётся такой агент: он входит
по открытому сигналу, стоп и цель берёт из сигнала, выходит, когда сигнал закрыт.
"""
from __future__ import annotations

from ..models import Action, Candle, Signal
from .base import Strategy, hold


class SignalFollower(Strategy):
    family = "signal_follower"
    description = "Повторяет сигналы проверенного внешнего источника: вход, стоп и цель берёт из сигнала, вне сигналов стоит в деньгах."
    side = "both"
    external = True            # источник решений вне компании: не переобучается и не отчисляется за простой

    @classmethod
    def default_params(cls) -> dict:
        return {"source": ""}

    def warmup(self) -> int:
        return 1

    def cadence_minutes(self) -> int:
        return 1

    def decide(self, candles: list[Candle], context: dict | None = None) -> Signal:
        ctx = context or {}
        exposure = float(ctx.get("exposure", 0.0))
        live = [s for s in (ctx.get("signals") or []) if s.get("status") == "open"]
        src = self.params.get("source") or "?"
        if not live:
            if abs(exposure) > 1e-9:
                return Signal(Action.SELL if exposure > 0 else Action.BUY, 0.0, 0.8, f"сигнал «{src}» закрыт: выхожу в деньги")
            return hold(f"открытых сигналов от «{src}» нет: жду", 0.0)
        s = live[-1]
        want = 1.0 if s["side"] == "long" else -1.0
        levels = f"вход {s['entry']:.0f}" + (f", стоп {s['stop']:.0f}" if s.get("stop") else "") + (f", цель {s['target']:.0f}" if s.get("target") else "")
        meta = {"stop": float(s.get("stop") or 0.0), "target": float(s.get("target") or 0.0), "signal_id": s.get("id")}
        if abs(exposure - want) < 0.05:
            return hold(f"по сигналу «{src}» ({levels}): держу", exposure)
        action = Action.BUY if want > 0 else Action.SELL
        return Signal(action, want, 0.7, f"сигнал «{src}»: {'лонг' if want > 0 else 'шорт'}, {levels}", meta)


EXTERNAL_STRATEGIES: list[type[Strategy]] = [SignalFollower]
