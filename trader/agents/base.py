"""Базовые классы: стратегия (мозг) и агент (стратегия + счёт + состояние)."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..models import Action, AgentSnapshot, Candle, Signal
from ..paper import PaperAccount


class Strategy(ABC):
    """Стратегия принимает историю свечей и возвращает сигнал.

    Стратегия не знает о деньгах: только «хочу быть в BTC на X%».
    """
    family: str = "abstract"
    description: str = ""

    def __init__(self, params: dict | None = None):
        self.params = {**self.default_params(), **(params or {})}

    @classmethod
    def default_params(cls) -> dict:
        return {}

    @classmethod
    def param_grid(cls) -> dict[str, list]:
        """Варианты параметров для отдела исследований."""
        return {}

    def warmup(self) -> int:
        """Сколько свечей нужно, чтобы стратегия начала выдавать сигналы."""
        return 50

    def cadence_minutes(self) -> int:
        """Как часто стратегия хочет смотреть на рынок (минуты). Каждая задаёт свой темп."""
        return 5

    @abstractmethod
    def decide(self, candles: list[Candle], context: dict | None = None) -> Signal:
        ...

    def uses_llm(self) -> bool:
        return False


@dataclass
class Agent:
    name: str
    strategy: Strategy
    account: PaperAccount
    status: str = "active"          # active | paused | fired | bench
    hired_at: int = field(default_factory=lambda: int(time.time()))
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    day_key: str = ""
    last_signal: Signal | None = None
    last_price: float = 0.0
    notes: list[str] = field(default_factory=list)   # уроки для LLM-агентов
    bars_in_position: int = 0
    last_ts_seen: int = 0
    last_decided_ts: int = 0     # когда агент последний раз принимал решение (unix)
    slot_minute: int = 0         # устаревшее: минута часа (осталось для совместимости БД)
    next_check_ts: int = 0       # когда агент хочет посмотреть на рынок в следующий раз
    alert_above: float = 0.0     # «разбуди, если цена выше» (нейро-агенты)
    alert_below: float = 0.0     # «разбуди, если цена ниже»
    last_logged_ts: int = 0
    last_target: float = -1.0
    stop_price: float = 0.0      # стоп-лосс текущей позиции (0 = нет позиции)

    def start_balance(self) -> float:
        return self._start_balance

    def __post_init__(self) -> None:
        if not self.slot_minute:
            h = 0
            for ch in self.name:
                h = (h * 31 + ord(ch)) & 0xFFFFFFFF
            self.slot_minute = 1 + h % 55
        self._start_balance = self.account.cash + self.account.btc * self.last_price
        self.peak_equity = self.peak_equity or self._start_balance
        self.day_start_equity = self.day_start_equity or self._start_balance

    def equity(self, price: float | None = None) -> float:
        return self.account.equity(price if price is not None else self.last_price)

    def drawdown(self, price: float | None = None) -> float:
        eq = self.equity(price)
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - eq) / self.peak_equity)

    def pnl_day(self, price: float | None = None) -> float:
        return self.equity(price) - self.day_start_equity

    def pnl_total(self, price: float | None = None) -> float:
        return self.equity(price) - self._start_balance

    def roll_day(self, day_key: str, price: float) -> bool:
        """Начало нового торгового дня. Возвращает True, если день сменился."""
        if day_key != self.day_key:
            self.day_key = day_key
            self.day_start_equity = self.equity(price)
            if self.status == "paused":
                self.status = "active"
            return True
        return False

    def observe(self, price: float) -> None:
        self.last_price = price
        self.peak_equity = max(self.peak_equity, self.equity(price))

    def after_trade_tick(self, price: float) -> None:
        self.bars_in_position = self.bars_in_position + 1 if self.account.btc > 0 else 0

    def reset_account(self, cash: float, ts: int) -> None:
        """Обнулить историю счёта (при повышении стажёра в команду)."""
        self.account.cash = cash
        self.account.btc = 0.0
        self.account.trades = []
        self.account.realized_pnl = 0.0
        self.account._avg_entry = 0.0
        self._start_balance = cash
        self.peak_equity = cash
        self.day_start_equity = cash
        self.hired_at = ts
        self.bars_in_position = 0

    def snapshot(self, price: float | None = None) -> AgentSnapshot:
        p = price if price is not None else self.last_price
        return AgentSnapshot(
            name=self.name,
            strategy=self.strategy.family,
            status=self.status,
            equity=round(self.equity(p), 2),
            cash=round(self.account.cash, 2),
            btc=round(self.account.btc, 6),
            exposure=round(self.account.exposure(p), 3),
            pnl_total=round(self.pnl_total(p), 2),
            pnl_day=round(self.pnl_day(p), 2),
            drawdown=round(self.drawdown(p), 4),
            trades=len(self.account.trades),
            win_rate=round(self.account.win_rate(), 3),
            hired_at=self.hired_at,
            params=dict(self.strategy.params),
            last_reason=(self.last_signal.reason if self.last_signal else ""),
            last_action=(self.last_signal.action.value if self.last_signal else ""),
            last_trade_ts=(self.account.trades[-1].ts if self.account.trades else 0),
            description=self.strategy.description,
            slot_minute=self.slot_minute,
            last_decided_ts=self.last_decided_ts,
            next_decision_ts=self.next_check_ts,
            cadence_minutes=self.strategy.cadence_minutes(),
            alert_above=self.alert_above,
            alert_below=self.alert_below,
            stop_price=self.stop_price,
        )


def hold(reason: str, exposure: float = 0.0) -> Signal:
    return Signal(Action.HOLD, exposure, 0.0, reason)
