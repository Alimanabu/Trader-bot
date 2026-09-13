"""Бумажный (демо) счёт. Реальные котировки, виртуальные деньги.

Спот: доля BTC от 0 до 100% капитала. Фьючерсный демо-режим (allow_short=True): позиция может быть
и отрицательной (шорт), но не больше 100% капитала по модулю, без плеча.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Trade


@dataclass
class PaperAccount:
    owner: str
    cash: float
    btc: float = 0.0                 # позиция в BTC; отрицательная = шорт
    fee_rate: float = 0.001
    slippage_rate: float = 0.0002
    min_order_usd: float = 10.0
    min_rebalance_frac: float = 0.05   # не дёргаться из-за перекоса меньше 5% капитала
    allow_short: bool = False
    funding_rate_8h: float = 0.0001    # ставка финансирования фьючерсов: 0.01% за 8 часов от размера позиции
    trades: list[Trade] = field(default_factory=list)
    realized_pnl: float = 0.0
    funding_paid: float = 0.0
    _avg_entry: float = 0.0

    def equity(self, price: float) -> float:
        return self.cash + self.btc * price

    def exposure(self, price: float) -> float:
        """Доля капитала в позиции: от -1 (шорт на всё) до 1 (лонг на всё)."""
        eq = self.equity(price)
        return 0.0 if eq <= 0 else (self.btc * price) / eq

    def rebalance(self, target_exposure: float, price: float, ts: int, reason: str = "") -> Trade | None:
        """Довести долю позиции до target_exposure. Возвращает сделку или None."""
        lo = -1.0 if self.allow_short else 0.0
        target_exposure = min(1.0, max(lo, target_exposure))
        eq = self.equity(price)
        if eq <= 0:
            return None
        delta = eq * target_exposure - self.btc * price
        if abs(delta) < max(self.min_order_usd, eq * self.min_rebalance_frac):
            return None
        if delta > 0:
            return self._buy(delta, price, ts, reason)
        return self._sell(-delta, price, ts, reason)

    # --- исполнение ---
    def _buy(self, usd: float, price: float, ts: int, reason: str) -> Trade | None:
        fill = price * (1 + self.slippage_rate)
        # покупка либо закрывает шорт (не требует наличных сверх залога), либо открывает/увеличивает лонг
        if self.btc >= 0:
            usd = min(usd, self.cash / (1 + self.fee_rate))
        if usd < self.min_order_usd:
            return None
        qty = usd / fill
        fee = usd * self.fee_rate
        pnl = cost = None
        if self.btc < 0:                                   # закрываем шорт (полностью или частично)
            close_qty = min(qty, -self.btc)
            pnl = (self._avg_entry - fill) * close_qty - fee * (close_qty / qty)
            cost = self._avg_entry * close_qty
            self.realized_pnl += pnl
            self.btc += close_qty
            rest = qty - close_qty
            self.cash -= close_qty * fill + fee * (close_qty / qty)
            if rest * fill >= self.min_order_usd:           # остаток открывает лонг (мелочь не открываем)
                self._avg_entry = fill + fee * (rest / qty) / rest
                self.btc += rest
                self.cash -= rest * fill + fee * (rest / qty)
            elif abs(self.btc) < 1e-12:
                self.btc, self._avg_entry = 0.0, 0.0
        else:
            total_cost = self._avg_entry * self.btc + fill * qty + fee   # комиссия входа в цене входа
            self.btc += qty
            self._avg_entry = total_cost / self.btc if self.btc else 0.0
            self.cash -= usd + fee
        t = Trade(ts, self.owner, "BUY", fill, qty, fee, reason, pnl=pnl, cost=cost, pos_after=self.btc)
        self.trades.append(t)
        return t

    def _sell(self, usd: float, price: float, ts: int, reason: str) -> Trade | None:
        fill = price * (1 - self.slippage_rate)
        qty = usd / fill
        if not self.allow_short:
            qty = min(qty, self.btc)
            if qty * fill < self.min_order_usd and qty < self.btc:
                return None
        else:
            # без плеча: |позиция| после продажи не больше капитала
            max_short_qty = max(0.0, (self.equity(price) / fill) + self.btc)
            qty = min(qty, max(0.0, self.btc) + max_short_qty)
        if qty <= 0 or qty * fill < self.min_order_usd:
            return None
        proceeds = qty * fill
        fee = proceeds * self.fee_rate
        pnl = cost = None
        if self.btc > 0:                                   # закрываем лонг (полностью или частично)
            close_qty = min(qty, self.btc)
            pnl = (fill - self._avg_entry) * close_qty - fee * (close_qty / qty)
            cost = self._avg_entry * close_qty
            self.realized_pnl += pnl
            self.btc -= close_qty
            self.cash += close_qty * fill - fee * (close_qty / qty)
            rest = qty - close_qty
            if rest * fill >= self.min_order_usd and self.allow_short:   # остаток открывает шорт (мелочь не открываем)
                self._avg_entry = fill - fee * (rest / qty) / rest
                self.btc -= rest
                self.cash += rest * fill - fee * (rest / qty)
            elif abs(self.btc) < 1e-12:
                self.btc, self._avg_entry = 0.0, 0.0
        else:                                              # открываем/увеличиваем шорт
            total = self._avg_entry * (-self.btc) + fill * qty - fee    # комиссия входа в цене входа шорта
            self.btc -= qty
            self._avg_entry = total / (-self.btc) if self.btc else 0.0
            self.cash += proceeds - fee
        t = Trade(ts, self.owner, "SELL", fill, qty, fee, reason, pnl=pnl, cost=cost, pos_after=self.btc)
        self.trades.append(t)
        return t

    def flatten(self, price: float, ts: int, reason: str = "flatten") -> Trade | None:
        if abs(self.btc) < 1e-12:
            return None
        if self.btc > 0:
            return self._sell(self.btc * price * 2, price, ts, reason)
        return self._buy(-self.btc * price * (1 + self.slippage_rate), price, ts, reason)

    def apply_funding(self, price: float, hours: float = 1.0, rate_8h: float | None = None) -> float:
        """Финансирование фьючерсов за прошедшие часы (только allow_short).

        Ставка со знаком, как на бирже: при положительной ставке лонги платят шортам,
        при отрицательной наоборот. Возвращает списанную сумму (отрицательная = получено).
        """
        if not self.allow_short or abs(self.btc) < 1e-12:
            return 0.0
        rate = self.funding_rate_8h if rate_8h is None else rate_8h
        charge = self.btc * price * rate * hours / 8.0     # знак позиции × знак ставки
        self.cash -= charge
        self.funding_paid += charge
        return charge

    def maintenance_ratio(self, price: float) -> float:
        """Капитал к размеру позиции: у фьючерсов при падении ниже порога позиция ликвидируется."""
        notional = abs(self.btc) * price
        return float("inf") if notional < 1e-9 else self.equity(price) / notional

    def liquidate(self, price: float, ts: int, fee_rate: float = 0.005) -> Trade | None:
        """Принудительное закрытие биржей: закрываем по рынку и платим ликвидационную комиссию."""
        t = self.flatten(price, ts, "ликвидация")
        if t:
            penalty = t.qty * t.price * fee_rate
            self.cash -= penalty
            t.fee += penalty
            if t.pnl is not None:
                t.pnl -= penalty
        return t

    def win_rate(self) -> float:
        closes = [t for t in self.trades if t.pnl is not None]
        if not closes:
            return 0.0
        return sum(1 for t in closes if t.pnl > 0) / len(closes)
