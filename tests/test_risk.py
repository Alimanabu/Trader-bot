from trader.agents.base import Agent
from trader.agents.rules import SmaCross
from trader.config import Settings
from trader.models import Action, Signal
from trader.paper import PaperAccount
from trader.risk import RiskManager


def make_agent(cash=1000.0, btc=0.0, price=100.0):
    a = Agent("t", SmaCross(), PaperAccount("t", cash=cash, btc=btc, fee_rate=0.0, slippage_rate=0.0))
    a.last_price = price
    a.roll_day("2026-01-01", price)
    a.observe(price)
    return a


def test_daily_loss_pauses():
    s = Settings(agent_daily_loss_limit=0.03)
    rm = RiskManager(s)
    a = make_agent(cash=0, btc=10, price=100.0)   # капитал 1000
    a.observe(96.0)                                # -4%
    v = rm.check_agent(a, Signal(Action.BUY, 1.0, 1.0, "x"), 96.0)
    assert v.pause and not v.allowed


def test_drawdown_fires():
    s = Settings(agent_max_drawdown=0.10, agent_daily_loss_limit=0.5)
    rm = RiskManager(s)
    a = make_agent(cash=0, btc=10, price=100.0)
    a.observe(120.0)   # пик 1200
    a.observe(105.0)   # просадка 12.5%
    v = rm.check_agent(a, Signal(Action.HOLD, 1.0, 1.0, "x"), 105.0)
    assert v.fire


def test_exposure_sized_by_risk_and_capped():
    s = Settings(agent_max_exposure=0.25, risk_per_trade=0.01, stop_atr_mult=2.0)
    rm = RiskManager(s)
    a = make_agent()
    # ATR 1% → стоп 2% → по риску 50%, потолок 25%
    v = rm.check_agent(a, Signal(Action.BUY, 1.0, 1.0, "x"), 100.0, atr_pct=0.01)
    assert v.allowed and abs(v.target_exposure - 0.25) < 1e-9
    # ATR 5% → стоп 10% → по риску 10%, потолок не мешает
    v = rm.check_agent(a, Signal(Action.BUY, 1.0, 1.0, "x"), 100.0, atr_pct=0.05)
    assert abs(v.target_exposure - 0.10) < 1e-9
    # стратегия хочет половину → половина от размера по риску
    v = rm.check_agent(a, Signal(Action.HOLD, 0.5, 1.0, "x"), 100.0, atr_pct=0.05)
    assert abs(v.target_exposure - 0.05) < 1e-9
    # буквальный потолок 10%
    rm2 = RiskManager(Settings(agent_max_exposure=0.10, risk_per_trade=0.5))
    assert rm2.check_agent(a, Signal(Action.BUY, 1.0, 1.0, "x"), 100.0, atr_pct=0.01).target_exposure == 0.10
    # значения по умолчанию: вход всем капиталом, ограничение риска выключено
    rm3 = RiskManager(Settings())
    assert rm3.check_agent(a, Signal(Action.BUY, 1.0, 1.0, "x"), 100.0, atr_pct=0.05).target_exposure == 1.0


def test_department_halt():
    rm = RiskManager(Settings(dept_daily_loss_limit=0.02))
    agents = [make_agent(cash=0, btc=10, price=100.0) for _ in range(3)]
    for a in agents:
        a.observe(97.0)
    ok, why = rm.check_department(agents, 97.0, "2026-01-01")
    assert not ok and "компании" in why
    ok2, _ = rm.check_department(agents, 100.0, "2026-01-02")
    assert ok2


def test_paused_trader_is_not_paused_again(settings):
    from tests.test_engine import make_engine
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    a = next(x for x in eng.agents if x.status == "active")
    a.account.cash -= a.equity(eng.last_price) * 0.05          # дневной убыток 5% > лимита 3%
    for k in range(6):
        eng.tick(now=T + 60 * (k + 1), force=True)
    assert a.status == "paused"
    pauses = [e for e in eng.j.recent_events(200) if e["kind"] == "pause" and e["agent"] == a.name]
    assert len(pauses) == 1


def test_llm_schemas_have_no_numeric_bounds():
    """API структурированных ответов не принимает minimum/maximum: запрос с ними падает с ошибкой 400."""
    import json
    from trader.analytics import VIEW_SCHEMA, RULE_SCHEMA, STRATEGY_SCHEMA
    from trader.agents.llm import DECISION_SCHEMA
    from trader.learning import LESSON_SCHEMA
    from trader.manager import REPORT_SCHEMA
    for schema in (VIEW_SCHEMA, RULE_SCHEMA, STRATEGY_SCHEMA, DECISION_SCHEMA, LESSON_SCHEMA, REPORT_SCHEMA):
        text = json.dumps(schema)
        for bad in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "multipleOf"):
            assert bad not in text
