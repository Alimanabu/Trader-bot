"""Реестр стратегий и сборка отдела по умолчанию."""
from __future__ import annotations

from ..config import Settings
from ..llm import ClaudeClient
from ..paper import PaperAccount
from .base import Agent, Strategy
from .llm import LLM_STRATEGIES
from .rules import RULE_STRATEGIES
from .sided import SHORTABLE, Sided

STRATEGY_FAMILIES: dict[str, type[Strategy]] = {s.family: s for s in RULE_STRATEGIES + LLM_STRATEGIES}
# семейства фьючерсного демо-режима: <база>_short и <база>_both
SIDED_FAMILIES: dict[str, tuple[str, str]] = {}
for _fam in SHORTABLE:
    for _side in ("short", "both"):
        SIDED_FAMILIES[f"{_fam}_{_side}"] = (_fam, _side)


def family_base(family: str) -> tuple[str, str]:
    """('sma_cross', 'short') для 'sma_cross_short'; ('sma_cross', 'long') для обычного."""
    if family in SIDED_FAMILIES:
        return SIDED_FAMILIES[family]
    return family, "long"


def all_families() -> list[str]:
    return list(STRATEGY_FAMILIES) + list(SIDED_FAMILIES)

DEFAULT_NAMES = {
    "sma_cross": "Тренд-1 (SMA)",
    "ema_momentum": "Импульс (EMA)",
    "rsi_reversion": "Контртренд (RSI)",
    "bollinger": "Боллинджер",
    "breakout": "Пробой (Donchian)",
    "macd": "MACD",
    "volume_spike": "Объёмник",
    "vol_regime": "Волатильность (ATR)",
    "llm_technician": "Нейро-технарь",
    "llm_regime": "Нейро-стратег",
    "llm_news": "Нейро-новостник",
    "sma_cross_short": "Медведь SMA",
    "macd_short": "Медведь MACD",
    "supertrend_short": "Медведь Supertrend",
    "ema_momentum_both": "Двусторонний EMA",
    "rsi_reversion_both": "Двусторонний RSI",
    "keltner_both": "Двусторонний Кельтнер",
    "llm_regime_both": "Нейро-двусторонний",
}

# Понятные имена для семейств, которые приходят из отдела исследований.
FAMILY_LABELS = {
    "zscore": "Z-score",
    "rsi_divergence": "Дивергенция RSI",
    "supertrend": "Supertrend",
    "keltner": "Кельтнер",
    "mtf": "Два таймфрейма",
    "seasonality": "Сезонность",
}


def family_label(family: str) -> str:
    if family in DEFAULT_NAMES:
        return DEFAULT_NAMES[family]
    base, side = family_base(family)
    label = DEFAULT_NAMES.get(base) or FAMILY_LABELS.get(base) or base
    if side == "short":
        return f"Медведь {label}"
    if side == "both":
        return f"Двусторонний {label}"
    return label


def build_strategy(family: str, params: dict | None = None, client: ClaudeClient | None = None) -> Strategy:
    base, side = family_base(family)
    cls = STRATEGY_FAMILIES[base]
    strat = cls(params, client=client) if cls in LLM_STRATEGIES else cls(params)
    return Sided(strat, side) if side != "long" else strat


def new_account(settings: Settings, owner: str, allow_short: bool = False) -> PaperAccount:
    return PaperAccount(owner=owner, cash=settings.agent_start_balance, fee_rate=settings.fee_rate,
                        slippage_rate=settings.slippage_rate, allow_short=allow_short)


def default_department(settings: Settings, client: ClaudeClient | None = None, hired_at: int | None = None) -> list[Agent]:
    agents: list[Agent] = []
    for family, name in DEFAULT_NAMES.items():
        strat = build_strategy(family, None, client)
        agent = Agent(name=name, strategy=strat, account=new_account(settings, name, allow_short=strat.side != "long"))
        if hired_at is not None:
            agent.hired_at = hired_at
        agents.append(agent)
    return agents
