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
    agent_daily_loss_limit: float = 0.03
    agent_max_drawdown: float = 0.10
    dept_daily_loss_limit: float = 0.02
    auto_hire: bool = True
    llm_model: str = "claude-opus-5"
    db_path: str = "data/trader.db"
    port: int = 8080
    panel_password: str = ""
    history_candles: int = 500
    bench_size: int = 3
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
        agent_daily_loss_limit=float(env.get("AGENT_DAILY_LOSS_LIMIT", "0.03")),
        agent_max_drawdown=float(env.get("AGENT_MAX_DRAWDOWN", "0.10")),
        dept_daily_loss_limit=float(env.get("DEPT_DAILY_LOSS_LIMIT", "0.02")),
        auto_hire=_bool(env.get("AUTO_HIRE"), True),
        llm_model=env.get("LLM_MODEL", "claude-opus-5"),
        db_path=env.get("DB_PATH", "data/trader.db"),
        port=int(env.get("PORT", "8080")),
        panel_password=env.get("PANEL_PASSWORD", ""),
        history_candles=int(env.get("HISTORY_CANDLES", "500")),
        bench_size=int(env.get("BENCH_SIZE", "3")),
        research_lookback=int(env.get("RESEARCH_LOOKBACK", "720")),
        retune_every_hours=int(env.get("RETUNE_EVERY_HOURS", "168")),
    )
