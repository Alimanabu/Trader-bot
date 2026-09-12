"""Настройки приложения. Читаются из переменных окружения и файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


@dataclass
class Settings:
    anthropic_api_key: str = ""
    market_source: str = "binance"
    symbol: str = "BTCUSDT"
    timeframe: str = "1h"
    agent_start_balance: float = 1000.0
    fee_rate: float = 0.001
    slippage_rate: float = 0.0002
    agent_max_exposure: float = 0.25   # потолок доли капитала агента в BTC на одну позицию
    risk_per_trade: float = 0.01       # сколько капитала агент готов потерять на одной сделке (1%)
    stop_atr_mult: float = 2.0         # стоп-лосс на расстоянии N·ATR от цены входа
    stop_cooldown_min: int = 30        # после стопа агент не входит заново столько минут
    agent_daily_loss_limit: float = 0.03
    agent_max_drawdown: float = 0.10
    dept_daily_loss_limit: float = 0.02
    auto_hire: bool = True
    llm_model: str = "claude-opus-5"
    db_path: str = "data/trader.db"
    port: int = 8080
    panel_password: str = ""
    history_candles: int = 800
    bench_size: int = 30
    team_size: int = 11
    intern_count: int = 20
    llm_min_interval_min: int = 60     # нейро-агент не может просить будить себя чаще (защита от расходов)
    llm_max_interval_min: int = 360
    llm_news_min_interval_min: int = 240   # новостник с веб-поиском дороже, поэтому реже
    llm_daily_budget_usd: float = 2.0      # жёсткий потолок расходов на нейросеть в сутки
    research_lookback: int = 720
    retune_every_hours: int = 168
    extra: dict = field(default_factory=dict)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    if env_file:
        _load_dotenv(Path(env_file))
    env = os.environ
    return Settings(
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
        market_source=env.get("MARKET_SOURCE", "binance").lower(),
        symbol=env.get("SYMBOL", "BTCUSDT"),
        timeframe=env.get("TIMEFRAME", "1h"),
        agent_start_balance=float(env.get("AGENT_START_BALANCE", "1000")),
        fee_rate=float(env.get("FEE_RATE", "0.001")),
        slippage_rate=float(env.get("SLIPPAGE_RATE", "0.0002")),
        agent_max_exposure=float(env.get("AGENT_MAX_EXPOSURE", "0.25")),
        risk_per_trade=float(env.get("RISK_PER_TRADE", "0.01")),
        stop_atr_mult=float(env.get("STOP_ATR_MULT", "2.0")),
        stop_cooldown_min=int(env.get("STOP_COOLDOWN_MIN", "30")),
        agent_daily_loss_limit=float(env.get("AGENT_DAILY_LOSS_LIMIT", "0.03")),
        agent_max_drawdown=float(env.get("AGENT_MAX_DRAWDOWN", "0.10")),
        dept_daily_loss_limit=float(env.get("DEPT_DAILY_LOSS_LIMIT", "0.02")),
        auto_hire=_bool(env.get("AUTO_HIRE"), True),
        llm_model=env.get("LLM_MODEL", "claude-opus-5"),
        db_path=env.get("DB_PATH", "data/trader.db"),
        port=int(env.get("PORT", "8080")),
        panel_password=env.get("PANEL_PASSWORD", ""),
        history_candles=int(env.get("HISTORY_CANDLES", "800")),
        bench_size=int(env.get("BENCH_SIZE", "30")),
        team_size=int(env.get("TEAM_SIZE", "11")),
        intern_count=int(env.get("INTERN_COUNT", "20")),
        llm_min_interval_min=int(env.get("LLM_MIN_INTERVAL_MIN", "60")),
        llm_max_interval_min=int(env.get("LLM_MAX_INTERVAL_MIN", "360")),
        llm_news_min_interval_min=int(env.get("LLM_NEWS_MIN_INTERVAL_MIN", "240")),
        llm_daily_budget_usd=float(env.get("LLM_DAILY_BUDGET_USD", "2.0")),
        research_lookback=int(env.get("RESEARCH_LOOKBACK", "720")),
        retune_every_hours=int(env.get("RETUNE_EVERY_HOURS", "168")),
    )
