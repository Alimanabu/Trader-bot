from trader.data.market import SyntheticMarket
from trader.engine import Engine
from trader.journal import Journal
from trader.llm import ClaudeClient


def make_engine(settings, seed=3, db=None):
    market = SyntheticMarket(seed=seed)
    return Engine(settings, market=market, journal=db or Journal(":memory:"), client=ClaudeClient(None)), market


def test_engine_creates_department(settings):
    eng, _ = make_engine(settings)
    assert len(eng.agents) == 10
    assert all(a.status == "active" for a in eng.agents)


def test_tick_records_decisions_and_skips_duplicate(settings):
    eng, market = make_engine(settings)
    res = eng.tick()
    assert res["ok"] and len(res["decisions"]) == 10
    again = eng.tick()
    assert again.get("skipped")
    market.advance(1)
    res2 = eng.tick()
    assert res2["ok"] and not res2.get("skipped")
    assert len(eng.j.recent_decisions(None, 100)) == 20


def test_state_persists_between_engines(settings):
    db = Journal(":memory:")
    eng, market = make_engine(settings, db=db)
    for _ in range(30):
        eng.tick()
        market.advance(1)
    equities = {a.name: round(a.equity(), 4) for a in eng.agents}
    eng2 = Engine(settings, market=market, journal=db, client=ClaudeClient(None))
    assert {a.name: round(a.equity(a.last_price), 4) for a in eng2.agents}.keys() == equities.keys()
    for a in eng2.agents:
        assert abs(a.account.cash - next(x for x in eng.agents if x.name == a.name).account.cash) < 1e-6
    assert eng2.last_tick_ts == eng.last_tick_ts


def test_team_size_stays_ten_after_firing(settings):
    settings.agent_max_drawdown = 0.001   # уволят почти всех сразу
    settings.agent_daily_loss_limit = 0.9
    settings.dept_daily_loss_limit = 0.9
    eng, market = make_engine(settings)
    for _ in range(12):
        eng.tick()
        market.advance(1)
    alive = [a for a in eng.agents if a.status != "fired"]
    fired = [a for a in eng.agents if a.status == "fired"]
    assert fired, "ожидались увольнения"
    assert len(alive) == 10
    kinds = {e["kind"] for e in eng.j.recent_events(200)}
    assert {"fire", "hire", "research"} <= kinds


def test_manual_approval_flow(settings):
    settings.auto_hire = False
    settings.agent_max_drawdown = 0.001
    settings.agent_daily_loss_limit = 0.9
    settings.dept_daily_loss_limit = 0.9
    eng, market = make_engine(settings)
    for _ in range(12):
        eng.tick()
        market.advance(1)
    pend = eng.j.pending_approvals()
    assert pend and pend[0]["kind"] == "hire"
    before = len([a for a in eng.agents if a.status != "fired"])
    eng.apply_approval(pend[0]["id"], True)
    assert len([a for a in eng.agents if a.status != "fired"]) == before + 1


def test_trades_restored_after_restart(settings):
    db = Journal(":memory:")
    eng, market = make_engine(settings, db=db)
    for _ in range(40):
        eng.tick()
        market.advance(1)
    counts = {a.name: len(a.account.trades) for a in eng.agents}
    assert any(counts.values())
    eng2 = Engine(settings, market=market, journal=db, client=ClaudeClient(None))
    assert {a.name: len(a.account.trades) for a in eng2.agents} == counts
    assert eng2.state()["department"]["equity"] == eng.state()["department"]["equity"]
