"""Общие структуры данных."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class Candle:
    ts: int          # unix-время открытия свечи, секунды
    open: float
    high: float
    low: float
    close: float
    volume: float


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class Signal:
    """Решение агента на одной свече.

    target_exposure — какую долю капитала агент хочет держать в BTC: от -1 (шорт на всё)
    до 1 (лонг на всё). Спотовые стратегии дают только 0..1, отрицательные значения возможны
    лишь у стратегий фьючерсного демо-режима.
    """
    action: Action
    target_exposure: float
    confidence: float
    reason: str
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.target_exposure = min(1.0, max(-1.0, float(self.target_exposure)))
        self.confidence = min(1.0, max(0.0, float(self.confidence)))


@dataclass
class Trade:
    ts: int
    agent: str
    side: str        # BUY / SELL
    price: float
    qty: float
    fee: float
    reason: str
    pnl: float | None = None     # результат закрытия позиции в $ (у открывающей сделки нет)
    cost: float | None = None    # стоимость закрытого по цене входа, чтобы считать %
    pos_after: float = 0.0       # позиция после сделки (BTC, отрицательная = шорт)


@dataclass
class AgentSnapshot:
    name: str
    strategy: str
    status: str
    equity: float
    cash: float
    btc: float
    exposure: float
    pnl_total: float
    pnl_day: float
    drawdown: float
    trades: int
    win_rate: float
    hired_at: int
    params: dict
    last_reason: str = ""
    last_action: str = ""
    last_trade_ts: int = 0
    description: str = ""
    slot_minute: int = 0
    last_decided_ts: int = 0
    next_decision_ts: int = 0
    cadence_minutes: int = 0
    alert_above: float = 0.0
    alert_below: float = 0.0
    stop_price: float = 0.0
    pnl_week: float = 0.0
    streak_weeks: int = 0
    trial_weeks: int = 0
    live_ready: bool = False
