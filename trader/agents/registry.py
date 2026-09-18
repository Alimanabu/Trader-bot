"""Реестр стратегий, дески и сборка компании по умолчанию.

Компания Botz состоит из трёх десков:
- быки (bulls): спот, только рост;
- медведи (bears): фьючерсный демосчёт, только падение;
- двусторонние (both): фьючерсный демосчёт, обе стороны.
Деск агента определяется стороной его стратегии.
"""
from __future__ import annotations

from ..config import Settings
from ..llm import ClaudeClient
from ..paper import PaperAccount
from .base import Agent, Strategy
from .llm import LLM_STRATEGIES
from .rules import RULE_STRATEGIES
from .scalper import EXPERIMENT_STRATEGIES
from .sided import SHORTABLE, Sided

STRATEGY_FAMILIES: dict[str, type[Strategy]] = {s.family: s for s in RULE_STRATEGIES + LLM_STRATEGIES + EXPERIMENT_STRATEGIES}
# экспериментальные агенты: вне десков, на своём счёте, для проверки идей на живых данных
EXPERIMENTS = {"scalper": "Скальпер"}
# семейства фьючерсного демо-режима: <база>_short и <база>_both
SIDED_FAMILIES: dict[str, tuple[str, str]] = {}
for _fam in SHORTABLE:
    for _side in ("short", "both"):
        SIDED_FAMILIES[f"{_fam}_{_side}"] = (_fam, _side)

DESKS = {
    "bulls": {"label": "Быки", "side": "long", "description": "Спот, только рост: покупают биткоин и выходят в доллары."},
    "bears": {"label": "Медведи", "side": "short", "description": "Фьючерсы без плеча, только падение: шорт и выход в деньги."},
    "both": {"label": "Двусторонние", "side": "both", "description": "Фьючерсы без плеча, обе стороны: лонг в росте, шорт в падении."},
}
DESK_BY_SIDE = {v["side"]: k for k, v in DESKS.items()}

# Звания (карьерная лестница): кандидат (скамейка) → стажёр → трейдер → старший трейдер → реальный счёт
RANK_INTERN, RANK_TRADER, RANK_SENIOR, RANK_LIVE = 0, 1, 2, 3
RANK_LABELS = {RANK_INTERN: "Стажёр", RANK_TRADER: "Трейдер", RANK_SENIOR: "Старший трейдер", RANK_LIVE: "Реальный счёт"}


def desk_of(side: str) -> str:
    return DESK_BY_SIDE.get(side, "bulls")


def family_base(family: str) -> tuple[str, str]:
    """('sma_cross', 'short') для 'sma_cross_short'; ('sma_cross', 'long') для обычного."""
    if family in SIDED_FAMILIES:
        return SIDED_FAMILIES[family]
    return family, "long"


def family_side(family: str) -> str:
    if family in STRATEGY_FAMILIES:
        return STRATEGY_FAMILIES[family].side
    return family_base(family)[1]


def all_families() -> list[str]:
    return [f for f in STRATEGY_FAMILIES if not f.startswith("llm_") and f not in EXPERIMENTS] + list(SIDED_FAMILIES)


# Штатные агенты по дескам (порядок важен: первые desk_size каждого деска попадают в команду при первом запуске)
DEFAULT_NAMES = {
    # быки
    "sma_cross": "Тренд-1 (SMA)",
    "ema_momentum": "Импульс (EMA)",
    "rsi_reversion": "Контртренд (RSI)",
    "bollinger": "Боллинджер",
    "breakout": "Пробой (Donchian)",
    "macd": "MACD",
    "volume_spike": "Объёмник",
    "vol_regime": "Волатильность (ATR)",
    # медведи
    "sma_cross_short": "Медведь SMA",
    "macd_short": "Медведь MACD",
    "supertrend_short": "Медведь Supertrend",
    "breakout_short": "Медведь Donchian",
    "bollinger_short": "Медведь Боллинджер",
    "keltner_short": "Медведь Кельтнер",
    # двусторонние
    "ema_momentum_both": "Двусторонний EMA",
    "rsi_reversion_both": "Двусторонний RSI",
    "keltner_both": "Двусторонний Кельтнер",
    "zscore_both": "Двусторонний Z-score",
    "mtf_both": "Двусторонний два ТФ",
    "supertrend_both": "Двусторонний Supertrend",
}

# Понятные имена для семейств, которые приходят из отдела исследований.
FAMILY_LABELS = {
    "zscore": "Z-score",
    "rsi_divergence": "Дивергенция RSI",
    "supertrend": "Supertrend",
    "keltner": "Кельтнер",
    "mtf": "Два таймфрейма",
    "seasonality": "Сезонность",
    "llm_technician": "Технический аналитик",
    "llm_regime": "Макро-стратег",
    "llm_news": "Новостной аналитик",
    "llm_regime_both": "Нейро-двусторонний",
    "scalper": "Скальпер (1 мин)",
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


def default_families(settings: Settings) -> list[tuple[str, str]]:
    """(семейство, имя) штатных агентов: по desk_size на каждый деск."""
    out: list[tuple[str, str]] = []
    per_desk: dict[str, int] = {}
    for family, name in DEFAULT_NAMES.items():
        desk = desk_of(family_side(family))
        if per_desk.get(desk, 0) >= settings.desk_size:
            continue
        per_desk[desk] = per_desk.get(desk, 0) + 1
        out.append((family, name))
    return out


def default_department(settings: Settings, client: ClaudeClient | None = None, hired_at: int | None = None) -> list[Agent]:
    agents: list[Agent] = []
    for family, name in default_families(settings):
        strat = build_strategy(family, None, client)
        agent = Agent(name=name, strategy=strat, account=new_account(settings, name, allow_short=strat.side != "long"))
        if hired_at is not None:
            agent.hired_at = hired_at
        agents.append(agent)
    return agents
