"""Отдел исследований: бэктест стратегий на истории и подбор кандидатов на скамейку запасных."""
from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass, asdict

from .agents.base import Strategy
from .agents.registry import STRATEGY_FAMILIES, build_strategy
from .agents.rules import RULE_STRATEGIES
from .models import Candle
from .paper import PaperAccount


@dataclass
class BacktestResult:
    family: str
    params: dict
    return_pct: float
    max_drawdown_pct: float
    sharpe: float
    trades: int
    bars: int

    def score(self) -> float:
        """Единая оценка: доходность с штрафом за просадку и бонусом за стабильность."""
        return self.return_pct - 0.5 * self.max_drawdown_pct + 2.0 * self.sharpe


def backtest(strategy: Strategy, candles: list[Candle], start_balance: float = 1000.0,
             fee_rate: float = 0.001, window: int = 300) -> BacktestResult:
    acc = PaperAccount(owner="bt", cash=start_balance, fee_rate=fee_rate)
    warm = max(strategy.warmup(), 2)
    equities: list[float] = []
    peak = start_balance
    max_dd = 0.0
    for i in range(warm, len(candles)):
        lo = max(0, i + 1 - max(window, warm + 5))
        view = candles[lo:i + 1]
        price = view[-1].close
        sig = strategy.decide(view, {"exposure": acc.exposure(price)})
        acc.rebalance(sig.target_exposure, price, view[-1].ts, sig.reason)
        eq = acc.equity(price)
        equities.append(eq)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak else 0.0)
    if not equities:
        return BacktestResult(strategy.family, dict(strategy.params), 0.0, 0.0, 0.0, 0, 0)
    rets = [(equities[k] / equities[k - 1] - 1) for k in range(1, len(equities)) if equities[k - 1] > 0]
    sharpe = 0.0
    if len(rets) > 2:
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
        sd = math.sqrt(var)
        if sd > 0:
            sharpe = m / sd * math.sqrt(24 * 365)   # часовые бары → годовой Sharpe
    return BacktestResult(strategy.family, dict(strategy.params), (equities[-1] / start_balance - 1) * 100,
                          max_dd * 100, sharpe, len(acc.trades), len(equities))


def grid(family: str, max_combos: int = 12, seed: int = 7) -> list[dict]:
    cls = STRATEGY_FAMILIES[family]
    g = cls.param_grid()
    if not g:
        return [cls.default_params()]
    keys = list(g)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*(g[k] for k in keys))]
    combos = [c for c in combos if _valid(family, c)]
    rng = random.Random(seed)
    if len(combos) > max_combos:
        combos = rng.sample(combos, max_combos)
    return combos


def _valid(family: str, p: dict) -> bool:
    if family == "sma_cross":
        return p["fast"] < p["slow"]
    if family == "macd":
        return p["fast"] < p["slow"]
    if family == "rsi_reversion":
        return p["oversold"] < p["overbought"]
    if family == "breakout":
        return p["exit"] <= p["entry"]
    return True


class StrategyLab:
    """Перебирает семейства и параметры, возвращает лучших кандидатов."""

    def __init__(self, fee_rate: float = 0.001, max_combos: int = 12):
        self.fee_rate = fee_rate
        self.max_combos = max_combos

    def research(self, candles: list[Candle], families: list[str] | None = None, top_n: int = 5,
                 exclude: set[str] | None = None) -> list[BacktestResult]:
        """exclude — ключи "семейство|params" уже работающих агентов, их не предлагаем повторно."""
        families = families or [s.family for s in RULE_STRATEGIES]
        results: list[BacktestResult] = []
        for fam in families:
            for params in grid(fam, self.max_combos):
                if exclude and combo_key(fam, params) in exclude:
                    continue
                strat = build_strategy(fam, params)
                results.append(backtest(strat, candles, fee_rate=self.fee_rate))
        results.sort(key=lambda r: r.score(), reverse=True)
        return results[:top_n]

    def families_count(self) -> list[str]:
        return [s.family for s in RULE_STRATEGIES]

    def best_params(self, family: str, candles: list[Candle]) -> BacktestResult:
        best: BacktestResult | None = None
        for params in grid(family, self.max_combos):
            r = backtest(build_strategy(family, params), candles, fee_rate=self.fee_rate)
            if best is None or r.score() > best.score():
                best = r
        assert best is not None
        return best


def combo_key(family: str, params: dict) -> str:
    import json
    return f"{family}|{json.dumps(params, sort_keys=True)}"


def result_to_dict(r: BacktestResult) -> dict:
    d = asdict(r)
    d["score"] = round(r.score(), 3)
    return d
