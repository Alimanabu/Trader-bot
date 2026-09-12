"""Реестр стратегий и сборка отдела по умолчанию."""
from __future__ import annotations

from ..config import Settings
from ..llm import ClaudeClient
from ..paper import PaperAccount
from .base import Agent, Strategy
from .llm import LLM_STRATEGIES
from .rules import RULE_STRATEGIES

STRATEGY_FAMILIES: dict[str, type[Strategy]] = {s.family: s for s in RULE_STRATEGIES + LLM_STRATEGIES}

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
    return DEFAULT_NAMES.get(family) or FAMILY_LABELS.get(family) or family


def build_strategy(family: str, params: dict | None = None, client: ClaudeClient | None = None) -> Strategy:
    cls = STRATEGY_FAMILIES[family]
    if cls in LLM_STRATEGIES:
        return cls(params, client=client)
    return cls(params)


def new_account(settings: Settings, owner: str) -> PaperAccount:
    return PaperAccount(owner=owner, cash=settings.agent_start_balance, fee_rate=settings.fee_rate,
                        slippage_rate=settings.slippage_rate)


def default_department(settings: Settings, client: ClaudeClient | None = None, hired_at: int | None = None) -> list[Agent]:
    agents: list[Agent] = []
    for family, name in DEFAULT_NAMES.items():
        strat = build_strategy(family, None, client)
        agent = Agent(name=name, strategy=strat, account=new_account(settings, name))
        if hired_at is not None:
            agent.hired_at = hired_at
        agents.append(agent)
    return agents
