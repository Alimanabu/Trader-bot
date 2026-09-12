"""Бумажный (демо) счёт. Реальные котировки, виртуальные деньги.

Спот, без плеча: агент может держать от 0% до 100% капитала в BTC.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Trade


@dataclass
class PaperAccount:
    owner: str
    cash: float
    btc: float = 0.0
    fee_rate: float = 0.001
    slippage_rate: float = 0.0002
    min_order_usd: float = 10.0
    min_rebalance_frac: float = 0.05   # не дёргаться из-за перекоса меньше 5% капитала
    trades: list[Trade] = field(default_factory=list)
    realized_pnl: float = 0.0
    _avg_entry: float = 0.0

    def equity(self, price: float) -> float:
        return self.cash + self.btc * price

    def exposure(self, price: float) -> float:
        eq = self.equity(price)
        return 0.0 if eq <= 0 else (self.btc * price) / eq

    def rebalance(self, target_exposure: float, price: float, ts: int, reason: str = "") -> Trade | None:
        """Довести долю BTC в портфеле до target_exposure (0..1). Возвращает сделку или None."""
        target_exposure = min(1.0, max(0.0, target_exposure))
        eq = self.equity(price)
        if eq <= 0:
            return None
        target_btc_value = eq * target_exposure
        current_value = self.btc * price
        delta = target_btc_value - current_value
        if abs(delta) < max(self.min_order_usd, eq * self.min_rebalance_frac):
            return None
        if delta > 0:
            return self._buy(delta, price, ts, reason)
        return self._sell(-delta, price, ts, reason)

    def _buy(self, usd: float, price: float, ts: int, reason: str) -> Trade | None:
        fill = price * (1 + self.slippage_rate)
        usd = min(usd, self.cash / (1 + self.fee_rate))
        if usd < self.min_order_usd:
            return None
        qty = usd / fill
        fee = usd * self.fee_rate
        total_cost = self._avg_entry * self.btc + fill * qty
        self.btc += qty
        self._avg_entry = total_cost / self.btc if self.btc else 0.0
        self.cash -= usd + fee
        t = Trade(ts, self.owner, "BUY", fill, qty, fee, reason)
        self.trades.append(t)
        return t

    def _sell(self, usd: float, price: float, ts: int, reason: str) -> Trade | None:
        fill = price * (1 - self.slippage_rate)
        qty = min(usd / fill, self.btc)
        if qty * fill < self.min_order_usd and qty < self.btc:
            return None
        if qty <= 0:
            return None
        proceeds = qty * fill
        fee = proceeds * self.fee_rate
        self.realized_pnl += (fill - self._avg_entry) * qty - fee
        self.btc -= qty
        if self.btc < 1e-12:
            self.btc = 0.0
            self._avg_entry = 0.0
        self.cash += proceeds - fee
        t = Trade(ts, self.owner, "SELL", fill, qty, fee, reason)
        self.trades.append(t)
        return t

    def flatten(self, price: float, ts: int, reason: str = "flatten") -> Trade | None:
        if self.btc <= 0:
            return None
        return self._sell(self.btc * price * 2, price, ts, reason)

    def win_rate(self) -> float:
        """Доля прибыльных продаж (грубая оценка по сделкам SELL)."""
        sells = [t for t in self.trades if t.side == "SELL"]
        if not sells:
            return 0.0
        wins = 0
        avg = 0.0
        qty_held = 0.0
        for t in self.trades:
            if t.side == "BUY":
                avg = (avg * qty_held + t.price * t.qty) / (qty_held + t.qty) if qty_held + t.qty else t.price
                qty_held += t.qty
            else:
                if t.price > avg:
                    wins += 1
                qty_held = max(0.0, qty_held - t.qty)
                if qty_held == 0:
                    avg = 0.0
        return wins / len(sells)
