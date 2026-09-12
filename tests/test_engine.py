from trader.data.market import SyntheticMarket
from trader.engine import Engine
from trader.journal import Journal
from trader.llm import ClaudeClient


def make_engine(settings, seed=3, db=None):
    market = SyntheticMarket(seed=seed)
    return Engine(settings, market=market, journal=db or Journal(":memory:"), client=ClaudeClient(None)), market


def hours(eng, market, n):
    """Прогнать n часов: каждый час новая свеча, время идёт явно (все агенты успевают проверить рынок)."""
    out = []
    for _ in range(n):
        last = market.candles("BTCUSDT", "1h", 1)[-1]
        out.append(eng.tick(now=last.ts + 3600 + 5))
        market.advance(1)
    return out


def test_engine_creates_department(settings):
    eng, _ = make_engine(settings)
    assert len(eng.agents) == 11
    assert all(a.status == "active" for a in eng.agents)


def test_tick_records_decisions_and_skips_duplicate(settings):
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    res = eng.tick(now=T)
    assert res["ok"] and len(res["decisions"]) == 11
    assert len(res["interns_added"]) == settings.intern_count
    eng.tick(now=T + 30)            # стажёры принимают первые решения
    again = eng.tick(now=T + 40)
    assert again.get("skipped")
    market.advance(1)
    res2 = eng.tick(now=T + 3600)
    # новостник с веб-поиском проверяет рынок не чаще раза в 4 часа, остальные десять успели
    assert res2["ok"] and not res2.get("skipped") and len(res2["decisions"]) == 10
    team_decisions = [d for d in eng.j.recent_decisions(None, 500) if d["agent"] in {a.name for a in eng.agents if a.status == "active"}]
    assert len(team_decisions) == 21    # первый проход + часовая контрольная запись


def test_state_persists_between_engines(settings):
    db = Journal(":memory:")
    eng, market = make_engine(settings, db=db)
    hours(eng, market, 30)
    equities = {a.name: round(a.equity(), 4) for a in eng.agents}
    eng2 = Engine(settings, market=market, journal=db, client=ClaudeClient(None))
    assert {a.name: round(a.equity(a.last_price), 4) for a in eng2.agents}.keys() == equities.keys()
    for a in eng2.agents:
        assert abs(a.account.cash - next(x for x in eng.agents if x.name == a.name).account.cash) < 1e-6
    assert eng2.last_tick_ts == eng.last_tick_ts


def force_drawdown(eng, names=None):
    """Имитировать просадку: поднять пик капитала, чтобы риск-менеджер уволил агента."""
    for a in eng.agents:
        if a.status in {"active", "paused"} and (names is None or a.name in names):
            a.peak_equity = a.equity(a.last_price) * 1.5


def test_team_size_stays_ten_after_firing(settings):
    settings.agent_daily_loss_limit = 0.9
    settings.dept_daily_loss_limit = 0.9
    eng, market = make_engine(settings)
    hours(eng, market, 1)
    force_drawdown(eng, {a.name for a in eng.agents[:4]})
    hours(eng, market, 3)
    team = [a for a in eng.agents if a.status in {"active", "paused"}]
    fired = [a for a in eng.agents if a.status == "fired"]
    assert fired, "ожидались увольнения"
    assert len(team) == 11
    kinds = {e["kind"] for e in eng.j.recent_events(500)}
    assert {"fire", "hire", "research", "intern"} <= kinds


def test_manual_approval_flow(settings):
    settings.auto_hire = False
    settings.agent_daily_loss_limit = 0.9
    settings.dept_daily_loss_limit = 0.9
    eng, market = make_engine(settings)
    hours(eng, market, 1)
    force_drawdown(eng, {eng.agents[0].name})
    hours(eng, market, 3)
    pend = eng.j.pending_approvals()
    assert pend and pend[0]["kind"] == "hire"
    before = len([a for a in eng.agents if a.status in {"active", "paused"}])
    eng.apply_approval(pend[0]["id"], True)
    assert len([a for a in eng.agents if a.status in {"active", "paused"}]) == before + 1


def test_trades_restored_after_restart(settings):
    db = Journal(":memory:")
    eng, market = make_engine(settings, db=db)
    hours(eng, market, 40)
    counts = {a.name: len(a.account.trades) for a in eng.agents}
    assert any(counts.values())
    eng2 = Engine(settings, market=market, journal=db, client=ClaudeClient(None))
    assert {a.name: len(a.account.trades) for a in eng2.agents} == counts
    assert eng2.state()["department"]["equity"] == eng.state()["department"]["equity"]


def test_interns_shadow_and_new_default_agent_backfilled(settings):
    settings.intern_count = 5
    db = Journal(":memory:")
    eng, market = make_engine(settings, db=db)
    hours(eng, market, 5)
    interns = [a for a in eng.agents if a.status == "intern"]
    assert len(interns) == 5
    st = eng.state()
    assert len(st["interns"]) == 5
    assert all(a["status"] != "intern" for a in st["agents"])
    assert st["department"]["start"] == 11 * settings.agent_start_balance
    # имитируем появление нового штатного семейства: удаляем запись из БД и перезагружаем
    db._exec("DELETE FROM agents WHERE strategy='llm_news'")
    eng2 = Engine(settings, market=market, journal=db, client=ClaudeClient(None))
    assert any(a.strategy.family == "llm_news" for a in eng2.agents)
    assert len([a for a in eng2.agents if a.status == "intern"]) == 5


def test_promotion_approval(settings):
    settings.intern_count = 3
    eng, market = make_engine(settings)
    hours(eng, market, 3)
    price = eng.last_price
    team = [a for a in eng.agents if a.status == "active"]
    worst = team[0]
    worst.hired_at -= 20 * 86400
    worst.account.cash -= 50   # в минусе
    intern = next(a for a in eng.agents if a.status == "intern")
    intern.hired_at -= 20 * 86400
    intern.account.cash += 30
    eng.j.kv_set("review_day", "")
    eng.head.review(eng.agents, price, eng.last_tick_ts)
    pend = [p for p in eng.j.pending_approvals() if p["kind"] == "promote"]
    assert pend and pend[0]["details"]["agent"] == worst.name and pend[0]["details"]["intern"] == intern.name
    eng.apply_approval(pend[0]["id"], True)
    assert worst.status == "fired"
    assert intern.status == "active"
    assert abs(intern.equity(price) - settings.agent_start_balance) < 1e-6


def test_agents_check_market_at_their_own_cadence(settings):
    settings.intern_count = 0
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    res = eng.tick(now=T)
    assert {d["agent"] for d in res["decisions"]} == {a.name for a in eng.agents}
    assert eng.tick(now=T + 30).get("skipped")
    res = eng.tick(now=T + 61)
    fast = {a.name for a in eng.agents if not a.strategy.uses_llm() and a.strategy.cadence_minutes() == 1}
    assert fast and {d["agent"] for d in res["decisions"]} == fast
    res = eng.tick(now=T + 10 * 60 + 1)
    expected = {a.name for a in eng.agents if not a.strategy.uses_llm() and a.strategy.cadence_minutes() <= 10}
    assert {d["agent"] for d in res["decisions"]} == expected
    # нейро-агенты без ключа ждут не меньше llm_min_interval_min и не больше llm_max
    llm = next(a for a in eng.agents if a.strategy.uses_llm())
    assert settings.llm_min_interval_min * 60 <= llm.next_check_ts - T <= settings.llm_max_interval_min * 60


def test_price_alert_wakes_llm_agent(settings):
    settings.intern_count = 0
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    llm = next(a for a in eng.agents if a.strategy.uses_llm())
    llm.alert_below = eng.last_price * 2      # цена уже ниже будильника
    res = eng.tick(now=T + 60)
    assert llm.name in {d["agent"] for d in res["decisions"]}


def test_quiet_checks_are_not_logged(settings):
    settings.intern_count = 0
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    breakout = next(a for a in eng.agents if a.strategy.family == "breakout")
    before = len(eng.j.recent_decisions(breakout.name, 500))
    for k in range(1, 20):
        eng.tick(now=T + k * 60)
    after = len(eng.j.recent_decisions(breakout.name, 500))
    assert after - before <= 3, "проверки без изменений не должны засорять журнал"


def test_stop_loss_closes_position_and_cools_down(settings):
    settings.intern_count = 0
    settings.stop_cooldown_min = 30
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    a = next((x for x in eng.agents if x.account.btc > 0), None)
    if a is None:   # на этом участке синтетики никто не купил — открываем позицию вручную
        a = eng.agents[0]
        t = a.account.rebalance(0.25, eng.last_price, T, "тест")
        eng._after_trade(a, t, eng.last_price, eng._atr_pct(eng.last_candles))
    assert a.stop_price > 0 and a.stop_price < eng.last_price
    # цена «падает» ниже стопа: подменяем котировку
    eng.market.price = lambda symbol: a.stop_price * 0.99
    res = eng.tick(now=T + 60)
    assert a.name in res["stops"]
    assert a.account.btc == 0 and a.stop_price == 0
    assert a.next_check_ts >= T + 60 + 30 * 60
    assert any(e["kind"] == "stop" for e in eng.j.recent_events(50))


def test_backfill_sell_pnl_for_old_trades():
    from trader.models import Trade
    j = Journal(":memory:")
    j.trade(Trade(1, "x", "BUY", 100.0, 1.0, 0.1, "b"))
    j.trade(Trade(2, "x", "SELL", 110.0, 1.0, 0.11, "s"))   # без pnl, как старые записи
    assert j.recent_trades(5)[0]["pnl"] is None
    assert j.backfill_trade_pnl() == 1
    t = j.recent_trades(5)[0]
    assert abs(t["pnl"] - (110.0 - 100.1 - 0.11)) < 1e-9 and abs(t["cost"] - 100.1) < 1e-9
