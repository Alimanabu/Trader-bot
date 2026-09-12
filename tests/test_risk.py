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


def test_department_halt():
    rm = RiskManager(Settings(dept_daily_loss_limit=0.02))
    agents = [make_agent(cash=0, btc=10, price=100.0) for _ in range(3)]
    for a in agents:
        a.observe(97.0)
    ok, why = rm.check_department(agents, 97.0, "2026-01-01")
    assert not ok and "отдела" in why
    ok2, _ = rm.check_department(agents, 100.0, "2026-01-02")
    assert ok2
