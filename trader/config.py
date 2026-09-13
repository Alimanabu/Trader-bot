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
    agent_max_exposure: float = 1.0    # потолок доли капитала агента в BTC (1.0 = входит всем)
    risk_per_trade: float = 1.0        # ограничение риска на сделку (1.0 = выключено, размер задаёт стратегия)
    stop_atr_mult: float = 2.0         # стоп-лосс на расстоянии N·ATR от цены входа
    stop_cooldown_min: int = 30        # после стопа агент не входит заново столько минут
    stop_slippage: float = 0.0005      # дополнительное проскальзывание на стопах (рыночный ордер в движении)
    liquidation_ratio: float = 0.05    # фьючерсы: капитал/позиция ниже этого → ликвидация
    agent_daily_loss_limit: float = 0.03
    agent_max_drawdown: float = 0.10
    dept_daily_loss_limit: float = 0.02
    auto_hire: bool = True
    llm_model: str = "claude-opus-5"
    llm_model_strong: str = ""             # сильная модель для редких важных вызовов (стратег, ревизор, отчёт директора); пусто = та же
    director_fail_weeks: int = 2           # недель подряд с рейтингом ниже 30 → предложение сменить директора
    director_bonus_pct: float = 10.0       # премия директора: % от недельной прибыли компании сверх равного распределения
    db_path: str = "data/trader.db"
    port: int = 8080
    panel_password: str = ""
    history_candles: int = 800
    bench_size: int = 30
    team_size: int = 18
    intern_count: int = 20
    llm_min_interval_min: int = 60     # устаревшее: границы темпа нейро-агентов (теперь нейросеть только в аналитике)
    llm_max_interval_min: int = 360
    llm_news_min_interval_min: int = 240
    llm_daily_budget_usd: float = 2.0      # жёсткий потолок расходов на нейросеть в сутки
    head_policy: bool = True               # директор распределяет капитал между десками по режиму рынка и отвечает за результат
    head_fail_weeks: int = 2               # столько недель подряд хуже «просто держать доллары» → защитный подход
    intern_idle_days: int = 3              # стажёр без единой сделки столько дней отчисляется
    team_idle_days: int = 7                # трейдер без сделок столько дней уходит в стажёры на недельной ротации
    weekly_demote_max: int = 2             # сколько худших трейдеров каждого деска за неделю можно перевести в стажёры
    live_ready_weeks: int = 3              # недель подряд в плюсе, чтобы стать кандидатом на реальный счёт
    senior_weeks: int = 2                  # недель подряд в плюсе, чтобы стать старшим трейдером
    analyst_interval_min: int = 240        # как часто аналитический отдел обновляет взгляд на рынок (минуты)
    analyst_news_interval_min: int = 480   # новостной аналитик с веб-поиском дороже, поэтому реже
    strategist_interval_h: int = 72        # как часто стратег развития пишет наблюдения и предложения
    memory_min_days: int = 10              # с какого объёма память по режиму влияет на решения директора
    research_lookback: int = 720
    retune_every_hours: int = 168
    extra: dict = field(default_factory=dict)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def desk_size(self) -> int:
        """Трейдеров на одном деске. Команда = три деска (быки, медведи, двусторонние)."""
        return max(1, self.team_size // 3)


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
        agent_max_exposure=float(env.get("AGENT_MAX_EXPOSURE", "1.0")),
        risk_per_trade=float(env.get("RISK_PER_TRADE", "1.0")),
        stop_atr_mult=float(env.get("STOP_ATR_MULT", "2.0")),
        stop_cooldown_min=int(env.get("STOP_COOLDOWN_MIN", "30")),
        stop_slippage=float(env.get("STOP_SLIPPAGE", "0.0005")),
        liquidation_ratio=float(env.get("LIQUIDATION_RATIO", "0.05")),
        agent_daily_loss_limit=float(env.get("AGENT_DAILY_LOSS_LIMIT", "0.03")),
        agent_max_drawdown=float(env.get("AGENT_MAX_DRAWDOWN", "0.10")),
        dept_daily_loss_limit=float(env.get("DEPT_DAILY_LOSS_LIMIT", "0.02")),
        auto_hire=_bool(env.get("AUTO_HIRE"), True),
        llm_model=env.get("LLM_MODEL", "claude-opus-5"),
        llm_model_strong=env.get("LLM_MODEL_STRONG", ""),
        director_fail_weeks=int(env.get("DIRECTOR_FAIL_WEEKS", "2")),
        director_bonus_pct=float(env.get("DIRECTOR_BONUS_PCT", "10")),
        db_path=env.get("DB_PATH", "data/trader.db"),
        port=int(env.get("PORT", "8080")),
        panel_password=env.get("PANEL_PASSWORD", ""),
        history_candles=int(env.get("HISTORY_CANDLES", "800")),
        bench_size=int(env.get("BENCH_SIZE", "30")),
        team_size=int(env.get("TEAM_SIZE", "18")),
        intern_count=int(env.get("INTERN_COUNT", "20")),
        llm_min_interval_min=int(env.get("LLM_MIN_INTERVAL_MIN", "60")),
        llm_max_interval_min=int(env.get("LLM_MAX_INTERVAL_MIN", "360")),
        llm_news_min_interval_min=int(env.get("LLM_NEWS_MIN_INTERVAL_MIN", "240")),
        llm_daily_budget_usd=float(env.get("LLM_DAILY_BUDGET_USD", "2.0")),
        head_policy=_bool(env.get("HEAD_POLICY"), True),
        head_fail_weeks=int(env.get("HEAD_FAIL_WEEKS", "2")),
        intern_idle_days=int(env.get("INTERN_IDLE_DAYS", "3")),
        team_idle_days=int(env.get("TEAM_IDLE_DAYS", "7")),
        weekly_demote_max=int(env.get("WEEKLY_DEMOTE_MAX", "2")),
        live_ready_weeks=int(env.get("LIVE_READY_WEEKS", "3")),
        senior_weeks=int(env.get("SENIOR_WEEKS", "2")),
        analyst_interval_min=int(env.get("ANALYST_INTERVAL_MIN", "240")),
        analyst_news_interval_min=int(env.get("ANALYST_NEWS_INTERVAL_MIN", "480")),
        strategist_interval_h=int(env.get("STRATEGIST_INTERVAL_H", "72")),
        memory_min_days=int(env.get("MEMORY_MIN_DAYS", "10")),
        research_lookback=int(env.get("RESEARCH_LOOKBACK", "720")),
        retune_every_hours=int(env.get("RETUNE_EVERY_HOURS", "168")),
    )
