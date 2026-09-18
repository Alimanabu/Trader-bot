"""Отдел исследований: бэктест стратегий на истории и подбор кандидатов на скамейку запасных.

Проверка на невиденных данных (walk-forward): история делится на две части. Первые две трети
используются для подбора параметров, последняя треть для проверки. Итоговая оценка кандидата
это худшая из двух, поэтому случайные совпадения с прошлым отбрасываются.
"""
from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass, asdict

from .agents.base import Strategy
from .agents.registry import STRATEGY_FAMILIES, SIDED_FAMILIES, build_strategy, family_base
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
    oos_return_pct: float | None = None     # результат на проверочном (невиденном) участке
    oos_drawdown_pct: float | None = None
    oos_sharpe: float | None = None
    oos_trades: int = 0

    @staticmethod
    def _score(ret: float, dd: float, sharpe: float) -> float:
        return ret - 0.5 * dd + 2.0 * sharpe

    def train_score(self) -> float:
        return self._score(self.return_pct, self.max_drawdown_pct, self.sharpe)

    def oos_score(self) -> float | None:
        if self.oos_return_pct is None:
            return None
        return self._score(self.oos_return_pct, self.oos_drawdown_pct or 0.0, self.oos_sharpe or 0.0)

    def score(self) -> float:
        """Единая оценка: доходность с штрафом за просадку и бонусом за стабильность.
        Если есть проверочный участок, берётся худшая из двух оценок."""
        oos = self.oos_score()
        return self.train_score() if oos is None else min(self.train_score(), oos)


def backtest(strategy: Strategy, candles: list[Candle], start_balance: float = 1000.0,
             fee_rate: float = 0.001, window: int = 300, curve: list | None = None) -> BacktestResult:
    acc = PaperAccount(owner="bt", cash=start_balance, fee_rate=fee_rate, allow_short=getattr(strategy, "side", "long") != "long")
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
        if acc.allow_short and (view[-1].ts + 3600) % 28800 == 0:
            acc.apply_funding(price, 8.0)
        eq = acc.equity(price)
        equities.append(eq)
        if curve is not None:
            curve.append({"ts": view[-1].ts, "equity": round(eq, 2), "price": price})
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
    family, _side = family_base(family)
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
    family, _ = family_base(family)
    if family == "sma_cross":
        return p["fast"] < p["slow"]
    if family == "macd":
        return p["fast"] < p["slow"]
    if family == "rsi_reversion":
        return p["oversold"] < p["overbought"]
    if family == "breakout":
        return p["exit"] <= p["entry"]
    return True


def walk_forward(strategy: Strategy, candles: list[Candle], fee_rate: float = 0.001, split: float = 0.67) -> BacktestResult:
    """Подбор на первых split истории, проверка на остатке. Если истории мало, обычный бэктест."""
    warm = max(strategy.warmup(), 2)
    n = len(candles)
    cut = int(n * split)
    if n - cut < warm + 48 or cut < warm + 48:
        return backtest(strategy, candles, fee_rate=fee_rate)
    train = backtest(strategy, candles[:cut], fee_rate=fee_rate)
    # проверочный участок видит хвост истории для разогрева индикаторов, но сделки считаются только на нём
    test = backtest(strategy, candles[max(0, cut - warm - 5):], fee_rate=fee_rate)
    train.oos_return_pct, train.oos_drawdown_pct = test.return_pct, test.max_drawdown_pct
    train.oos_sharpe, train.oos_trades = test.sharpe, test.trades
    return train


class StrategyLab:
    """Перебирает семейства и параметры, возвращает лучших кандидатов."""

    def __init__(self, fee_rate: float = 0.001, max_combos: int = 12, max_per_family: int = 2, walk_forward: bool = True):
        self.fee_rate = fee_rate
        self.max_combos = max_combos
        self.max_per_family = max_per_family
        self.walk_forward = walk_forward

    def evaluate(self, strategy: Strategy, candles: list[Candle]) -> BacktestResult:
        return walk_forward(strategy, candles, self.fee_rate) if self.walk_forward else backtest(strategy, candles, fee_rate=self.fee_rate)

    def research(self, candles: list[Candle], families: list[str] | None = None, top_n: int = 5,
                 exclude: set[str] | None = None) -> list[BacktestResult]:
        """exclude — ключи "семейство|params" уже работающих агентов, их не предлагаем повторно."""
        families = families or self.families_count()
        results: list[BacktestResult] = []
        for fam in families:
            for params in grid(fam, self.max_combos):
                if exclude and combo_key(fam, params) in exclude:
                    continue
                strat = build_strategy(fam, params)
                results.append(self.evaluate(strat, candles))
        results.sort(key=lambda r: r.score(), reverse=True)
        # разнообразие: не больше max_per_family кандидатов одного семейства
        picked: list[BacktestResult] = []
        per_fam: dict[str, int] = {}
        for r in results:
            if per_fam.get(r.family, 0) >= self.max_per_family:
                continue
            picked.append(r)
            per_fam[r.family] = per_fam.get(r.family, 0) + 1
            if len(picked) >= top_n:
                break
        return picked

    def families_count(self) -> list[str]:
        return [s.family for s in RULE_STRATEGIES] + list(SIDED_FAMILIES)

    def best_params(self, family: str, candles: list[Candle]) -> BacktestResult:
        best: BacktestResult | None = None
        for params in grid(family, self.max_combos):
            r = self.evaluate(build_strategy(family, params), candles)
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
    d["train_score"] = round(r.train_score(), 3)
    oos = r.oos_score()
    d["oos_score"] = None if oos is None else round(oos, 3)
    return d
