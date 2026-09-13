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
    assert len(eng.agents) == settings.team_size == 18
    assert all(a.status == "active" for a in eng.agents)
    assert sum(1 for a in eng.agents if a.strategy.side == "short") == 6
    assert sum(1 for a in eng.agents if a.strategy.side == "both") == 6
    assert all(a.desk in {"bulls", "bears", "both"} and a.rank == 1 for a in eng.agents)


def test_tick_records_decisions_and_skips_duplicate(settings):
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    res = eng.tick(now=T)
    assert res["ok"] and len(res["decisions"]) == 18
    assert len(res["interns_added"]) == settings.intern_count
    eng.tick(now=T + 30)            # стажёры принимают первые решения
    again = eng.tick(now=T + 40)
    assert again.get("skipped")
    market.advance(1)
    res2 = eng.tick(now=T + 3600)
    assert res2["ok"] and not res2.get("skipped") and len(res2["decisions"]) == 18
    team_decisions = [d for d in eng.j.recent_decisions(None, 500) if d["agent"] in {a.name for a in eng.agents if a.status == "active"}]
    assert len(team_decisions) == 36    # первый проход + часовая контрольная запись


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
    assert len(team) == settings.team_size
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
    assert st["department"]["start"] == settings.team_size * settings.agent_start_balance
    # имитируем появление нового штатного семейства: удаляем запись из БД и перезагружаем
    db._exec("DELETE FROM agents WHERE name='Медведь Supertrend'")
    eng2 = Engine(settings, market=market, journal=db, client=ClaudeClient(None))
    assert any(a.strategy.family == "supertrend_short" for a in eng2.agents)
    assert len([a for a in eng2.agents if a.status == "intern"]) == 5


def test_weekly_rotation(settings):
    settings.intern_count = 6
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)                       # первая неделя зафиксирована
    price = eng.last_price
    team = [a for a in eng.agents if a.status == "active"]
    worst, streaky = team[0], team[1]
    worst.account.cash -= 50              # минус за неделю
    streaky.account.cash += 20            # плюс за неделю, уже 2 недели в серии
    streaky.streak_weeks = 2
    intern = next(a for a in eng.agents if a.status == "intern" and a.desk == worst.desk)
    intern.account.cash += 30             # лучший стажёр недели на том же деске
    loser_intern = [a for a in eng.agents if a.status == "intern" and a is not intern][0]
    loser_intern.account.cash -= 10
    loser_intern.streak_weeks = -1        # уже одна неделя в минусе
    res = eng.tick(now=T + 7 * 86400)
    w = res["weekly"]
    assert worst.name in w["demoted"] and worst.status == "intern" and worst.trial_weeks == 1
    assert intern.name in w["promoted"] and intern.status == "active" and abs(intern.start_balance() - settings.agent_start_balance) < 1e-6
    assert streaky.name in w["live_ready"] and streaky.live_ready and streaky.streak_weeks == 3 and streaky.rank == 2
    assert loser_intern.name in w["dropped"] and loser_intern.status == "dropped"
    assert any(p["kind"] == "live" and p["details"]["agent"] == streaky.name for p in eng.j.pending_approvals())
    assert len([a for a in eng.agents if a.status in {"active", "paused"}]) == settings.team_size
    kinds = {e["kind"] for e in eng.j.recent_events(100)}
    assert {"weekly", "demote", "live_ready", "drop"} <= kinds


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
    assert not any(a.strategy.uses_llm() for a in eng.agents), "нейросеть больше не торгует"


def test_price_alert_wakes_agent(settings):
    settings.intern_count = 0
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    slow = max(eng.agents, key=lambda a: a.strategy.cadence_minutes())
    slow.alert_below = eng.last_price * 2      # цена уже ниже будильника
    res = eng.tick(now=T + 60)
    assert slow.name in {d["agent"] for d in res["decisions"]}


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


def test_head_regime_caps_and_mode_switch(settings):
    settings.intern_count = 0
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    pol = eng.head.policy()
    assert pol["regime"] in {"up", "flat", "down"} and 0 < pol["caps"]["bulls"] <= 1.0
    assert eng.risk.desk_caps == pol["caps"]
    # режим определяется по свечам
    from trader.models import Candle
    up = [Candle(i * 3600, 100 + i, 101 + i, 99 + i, 100 + i, 1.0) for i in range(260)]
    down = [Candle(i * 3600, 400 - i, 401 - i, 399 - i, 400 - i, 1.0) for i in range(260)]
    assert eng.head.assess_regime(up) == "up" and eng.head.assess_regime(down) == "down"
    # две недели хуже долларов → защитный подход
    for a in eng.agents:
        if a.status == "active":
            a.account.cash -= 30
    eng.tick(now=T + 7 * 86400)
    for a in eng.agents:
        if a.status == "active":
            a.account.cash -= 30
    eng.tick(now=T + 14 * 86400)
    pol = eng.head.policy()
    assert pol["fail_weeks"] >= 2 and pol["mode"] == "defensive"
    assert pol["caps"]["bulls"] <= 0.6 and pol["alloc_weeks"] == 2
    assert "equal" in pol["weeks"][-1] and "desks" in pol["weeks"][-1]
    kinds = [e for e in eng.j.recent_events(200) if e["kind"] == "head"]
    assert kinds and any("защитный" in e["message"] for e in kinds)


def test_idle_interns_are_dropped_and_replaced(settings):
    settings.intern_count = 3
    settings.intern_idle_days = 3
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    interns = [a for a in eng.agents if a.status == "intern"]
    assert len(interns) == 3
    # трое суток без единой сделки → отчисление на ежедневном разборе и замена
    for a in interns:
        a.account.trades = []
        a.hired_at = T - 4 * 86400
        a.next_check_ts = T + 10 * 86400      # чтобы на этом проходе они не успели сделать сделку
    eng.j.kv_set("review_day", "")
    market.advance(1)
    eng.tick(now=T + 3600)
    dropped = [a for a in interns if a.status == "dropped"]
    assert len(dropped) == 3
    assert len([a for a in eng.agents if a.status == "intern"]) == 3
    assert any(e["kind"] == "drop" and "нет сделок" in e["message"] for e in eng.j.recent_events(100))


def test_research_limits_same_family(candles):
    from trader.research import StrategyLab
    lab = StrategyLab(max_combos=6, max_per_family=2)
    res = lab.research(candles[-300:], families=["breakout", "keltner"], top_n=6)
    fams = [r.family for r in res]
    assert fams.count("breakout") <= 2 and fams.count("keltner") <= 2


def test_short_stop_loss_above_entry(settings):
    settings.intern_count = 0
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    bear = next(a for a in eng.agents if a.strategy.side == "short")
    bear.account.flatten(eng.last_price, T, "тест")
    t = bear.account.rebalance(-1.0, eng.last_price, T, "тест шорт")
    assert t and t.side == "SELL" and bear.account.btc < 0
    eng._after_trade(bear, t, eng.last_price, eng._atr_pct(eng.last_candles))
    assert bear.stop_price > eng.last_price
    eng.market.price = lambda symbol: bear.stop_price * 1.01
    res = eng.tick(now=T + 60)
    assert bear.name in res["stops"] and abs(bear.account.btc) < 1e-9
    closing = bear.account.trades[-1]
    assert closing.side == "BUY" and closing.pnl is not None and closing.pnl < 0


def test_liquidation_and_signed_funding(settings):
    from trader.paper import PaperAccount
    f = PaperAccount("f", cash=1000, fee_rate=0.001, slippage_rate=0.0, allow_short=True)
    f.rebalance(-1.0, 100.0, 1)
    # положительная ставка: шорт получает; отрицательная: шорт платит
    assert f.apply_funding(100.0, 8.0, rate_8h=0.0001) < 0
    assert f.apply_funding(100.0, 8.0, rate_8h=-0.0001) > 0
    # цена почти удвоилась: капитал к позиции ниже порога → ликвидация
    assert f.maintenance_ratio(195.0) < 0.05
    t = f.liquidate(195.0, 2)
    assert t and t.side == "BUY" and abs(f.btc) < 1e-9 and t.pnl < 0


def test_engine_liquidation_check(settings):
    settings.intern_count = 0
    settings.dept_daily_loss_limit = 0.99      # иначе отдел остановится раньше, чем сработает ликвидация
    settings.agent_daily_loss_limit = 0.99
    settings.agent_max_drawdown = 0.99
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    bear = next(a for a in eng.agents if a.strategy.side == "short")
    bear.account.flatten(eng.last_price, T, "тест")
    bear.account.rebalance(-1.0, eng.last_price, T, "тест")
    bear.stop_price = 0.0
    eng.market.price = lambda symbol: bear.account._avg_entry * 1.97
    res = eng.tick(now=T + 60)
    assert bear.name in res["liquidations"] and abs(bear.account.btc) < 1e-9
    assert any(e["kind"] == "liquidation" for e in eng.j.recent_events(50))


def test_head_caps_are_side_aware(settings):
    from trader.models import Candle
    settings.intern_count = 0
    eng, market = make_engine(settings)
    down = [Candle(i * 3600, 400 - i, 401 - i, 399 - i, 400 - i, 1.0) for i in range(260)]
    pol = eng.head.daily_policy(down, down[-1].ts + 3600)
    assert pol["regime"] == "down" and pol["caps"]["bulls"] < pol["caps"]["bears"] == 1.0


def test_director_combines_rules_and_analysts():
    from trader.manager import Director
    strong_up = {"regime": "up", "strength": 0.8, "fresh": True, "votes": {}}
    weak = {"regime": "up", "strength": 0.4, "fresh": True, "votes": {}}
    assert Director.combine("flat", strong_up)[0] == "up"
    assert Director.combine("flat", weak)[0] == "flat"
    assert Director.combine("down", strong_up)[0] == "flat"
    assert Director.combine("up", strong_up)[0] == "up"
    assert Director.combine("down", None)[0] == "down"


def test_llm_agents_moved_to_analytics_on_load(settings):
    import json
    db = Journal(":memory:")
    eng, market = make_engine(settings, db=db)
    hours(eng, market, 1)
    # старая база: нейро-агент в команде
    db._exec("INSERT INTO agents(name,strategy,params,status,hired_at,cash,btc,peak_equity,day_start_equity,day_key,start_balance) "
             "VALUES('Нейро-стратег','llm_regime','{}','active',1,1000,0,1000,1000,'',1000)")
    eng2 = Engine(settings, market=market, journal=db, client=ClaudeClient(None))
    assert not any(a.strategy.uses_llm() for a in eng2.agents)
    assert any(e["kind"] == "analytics" and "аналитический" in e["message"] for e in db.recent_events(50))
    assert len([a for a in eng2.agents if a.status == "active"]) == settings.team_size


def test_overfull_desk_is_trimmed_on_load(settings):
    db = Journal(":memory:")
    eng, market = make_engine(settings, db=db)
    hours(eng, market, 1)
    db._exec("INSERT INTO agents(name,strategy,params,status,hired_at,cash,btc,peak_equity,day_start_equity,day_key,start_balance) "
             "VALUES('Лишний бык','vol_regime','{}','active',1,900,0,1000,1000,'',1000)")
    eng2 = Engine(settings, market=market, journal=db, client=ClaudeClient(None))
    bulls = [a for a in eng2.agents if a.desk == "bulls" and a.status == "active"]
    assert len(bulls) == settings.desk_size
    assert next(a for a in eng2.agents if a.name == "Лишний бык").status == "intern"


def test_analytics_views_scoring_and_consensus(settings):
    from trader.analytics import AnalyticsDept
    j = Journal(":memory:")
    dept = AnalyticsDept(settings, j, ClaudeClient(None))
    assert dept.run_due([], 100.0, 1000) == []          # без ключа отдел молчит
    j.add_view(1000, "Технический аналитик", "up", 0.9, "растём", 100.0)
    j.add_view(1000, "Макро-стратег", "down", 0.5, "падаем", 100.0)
    c = dept.consensus(2000)
    assert c["regime"] == "up" and c["fresh"] and c["strength"] > 0.6
    assert j.score_views(103.0, 1000 + 25 * 3600) == 2
    st = j.analyst_stats()
    assert st["Технический аналитик"]["hits"] == 1 and st["Макро-стратег"]["hits"] == 0
    assert dept.stats()[0]["accuracy"] == 1.0


def test_company_memory_grows_daily_and_shapes_director(settings):
    settings.intern_count = 3
    settings.memory_min_days = 2
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    # два дня подряд: итоги дня попадают в память семейств и десков под режим рынка
    for k in (1, 2):
        for a in eng.agents:
            if a.status == "active" and a.desk == "bears":
                a.account.cash -= 60          # медведи стабильно теряют
        market.advance(24)
        eng.tick(now=T + k * 86400)
    mem = eng.j.memory_table("desk")
    bears = [m for m in mem if m["key"] == "bears"]
    assert bears and sum(m["days"] for m in bears) == 2 and all(m["avg"] < 0 for m in bears)
    assert any(m["scope"] == "family" for m in eng.j.memory_table())
    assert any(e["kind"] == "knowledge" for e in eng.j.recent_events(100))
    # директор режет капитал деску, который в этом режиме теряет
    regime = bears[0]["regime"]
    caps = dict(eng.head.ALLOC["balanced"][regime])
    notes = eng.head.memory_adjust(caps, regime)
    assert notes and caps["bears"] < eng.head.ALLOC["balanced"][regime]["bears"]
    # память переживает людей: семейство помнится независимо от имени агента
    fam = next(a.strategy.family for a in eng.agents if a.desk == "bears" and a.status == "active")
    assert eng.head.memory_bias(fam, regime) == -1


def test_rule_approval_flows_into_risk_manager(settings):
    settings.intern_count = 0
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    aid = eng.j.request_approval("rule", "Ревизор предлагает правило: нет новых входов в 03 UTC",
                                 {"rule": {"type": "no_entry_hours", "hours": list(range(24)), "desk": ""}, "text": "нет новых входов", "rationale": "тест"}, ts=T)
    eng.apply_approval(aid, True)
    assert eng.j.active_rules() and eng.risk.rules
    # правило действует: никто не может увеличить позицию, даже если стратегия требует входа
    from trader.models import Action, Signal
    for a in eng.agents:
        a.account.flatten(eng.last_price, T, "тест")
        a.last_target = 0.0
    eng.agents[0].strategy.decide = lambda candles, ctx=None: Signal(Action.BUY, 1.0, 1.0, "тест: хочу купить")
    res = eng.tick(now=T + 3600, force=True)
    assert all(abs(a.account.btc) < 1e-9 for a in eng.agents)
    assert any("правило" in d["reason"] for d in eng.j.recent_decisions(None, 200))
    assert eng.j.active_rules()[0]["uses"] > 0
    assert any(e["kind"] == "rule" for e in eng.j.recent_events(50))


def test_proposal_approval_updates_knowledge(settings):
    settings.intern_count = 0
    eng, _ = make_engine(settings)
    kid = eng.j.add_knowledge(1, "proposal", "product", "Добавить экран уведомлений", "Стратег развития", {"kind": "product"}, status="pending")
    aid = eng.j.request_approval("proposal", "Стратег предлагает", {"knowledge_id": kid, "kind": "product"}, ts=1)
    eng.apply_approval(aid, True)
    assert eng.j.knowledge_by_id(kid)["status"] == "accepted"
    kid2 = eng.j.add_knowledge(2, "proposal", "risk", "Снизить стоп", "Стратег развития", {"kind": "risk"}, status="pending")
    aid2 = eng.j.request_approval("proposal", "Стратег предлагает", {"knowledge_id": kid2, "kind": "risk"}, ts=2)
    eng.apply_approval(aid2, False)
    assert eng.j.knowledge_by_id(kid2)["status"] == "rejected"
    st = eng.state()["knowledge"]
    assert st["counts"]["proposal"]["accepted"] == 1 and len(st["proposals"]) == 2


def test_lesson_verification_retires_useless_lessons(settings):
    from trader.analytics import AnalyticsDept
    j = Journal(":memory:")
    dept = AnalyticsDept(settings, j, ClaudeClient(None))
    name = "Макро-стратег"
    # до урока: 4 из 5 попаданий; после урока: 1 из 5
    for i in range(5):
        j.add_view(1000 + i * 3600, name, "up", 0.8, "x", 100.0)
    j.score_views(101.0, 1000 + 5 * 3600 + 25 * 3600)
    j._exec("UPDATE views SET hit=1"); j._exec("UPDATE views SET hit=0 WHERE id=1")
    L = 1000 + 6 * 3600
    kid = j.add_knowledge(L, "lesson", name, "не спеши со ставкой на рост", "наставник")
    for i in range(5):
        j.add_view(L + 100 + i * 3600, name, "up", 0.8, "y", 100.0)
    j.score_views(99.0, L + 100 + 5 * 3600 + 25 * 3600)
    res = dept.verify_lessons(L + 100 + 5 * 3600 + 26 * 3600)
    assert res and not res[0]["kept"] and j.knowledge_by_id(kid)["status"] == "retired"
    assert dept.lessons_for(name) == []


def test_walk_forward_scores_out_of_sample(candles):
    from trader.research import StrategyLab, walk_forward
    from trader.agents.registry import build_strategy
    r = walk_forward(build_strategy("sma_cross"), candles)
    assert r.oos_return_pct is not None and r.score() <= r.train_score()
    lab = StrategyLab(max_combos=3)
    best = lab.best_params("breakout", candles[-300:])
    assert best.oos_score() is not None


def test_backfill_does_not_treat_short_open_as_close():
    from trader.models import Trade
    j = Journal(":memory:")
    j._exec("INSERT INTO agents(name,strategy,params,status,hired_at,cash,btc,peak_equity,day_start_equity,day_key,start_balance) "
            "VALUES('Медведь X','sma_cross_short','{}','active',1,1000,0,1000,1000,'',1000)")
    j.trade(Trade(1, "Медведь X", "SELL", 100.0, 1.0, 0.1, "шорт"))      # открытие шорта: итога нет
    j.trade(Trade(2, "Медведь X", "BUY", 90.0, 1.0, 0.09, "закрыл шорт"))
    assert j.backfill_trade_pnl() == 0
    assert j.recent_trades(5)[1]["pnl"] is None
    # старая ошибка: итог открытия шорта записан как сумма продажи
    j._exec("UPDATE trades SET pnl=99.9, cost=0 WHERE id=1")
    assert j.repair_short_pnl() >= 1
    rows = {t["id"]: t for t in j.recent_trades(5)}
    assert rows[1]["pnl"] is None
    assert abs(rows[2]["pnl"] - ((100.0 - 0.1) - 90.0 - 0.09)) < 1e-9
    assert j.repair_short_pnl() == 0            # второй раз не трогает


def test_director_rating_bonus_and_replacement(settings):
    settings.intern_count = 0
    settings.director_fail_weeks = 2
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    d0 = eng.head.director()
    assert d0["number"] == 1 and d0["rating"] == 50 and d0["style"] == "balanced"
    # хорошая неделя: компания в плюсе → рейтинг растёт, премия положительная
    for a in eng.agents:
        if a.status == "active":
            a.account.cash += 40
    eng.tick(now=T + 7 * 86400)
    d1 = eng.head.director()
    assert d1["rating"] > 50 and d1["bonus"] > 0 and d1["weeks"] == 1
    # плохие недели подряд → рейтинг ниже 30 две недели → заявка на смену директора
    pend = []
    for k in range(2, 12):
        for a in eng.agents:
            if a.status == "active":
                a.account.cash -= 60
        eng.tick(now=T + k * 7 * 86400)
        pend = [p for p in eng.j.pending_approvals() if p["kind"] == "director"]
        if pend:
            break
    d2 = eng.head.director()
    assert d2["rating"] < 30 and d2["bonus"] < d1["bonus"] and d2["low_weeks"] >= 2
    assert pend and pend[0]["details"]["next_style"] == "aggressive"
    eng.apply_approval(pend[0]["id"], True)
    d3 = eng.head.director()
    assert d3["number"] == 2 and d3["style"] == "aggressive" and d3["rating"] == 50
    assert eng.j.knowledge("director", "retired")[0]["topic"] == "Директор №1"
    assert eng.head.alloc() is eng.head.ALLOC_STYLES["aggressive"]
    st = eng.state()
    assert st["head"]["director"]["number"] == 2 and st["directors_history"]


def test_open_positions_monitor(settings):
    settings.intern_count = 0
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    for a in eng.agents:
        a.account.flatten(eng.last_price, T, "тест")
    bull = next(a for a in eng.agents if a.desk == "bulls")
    bear = next(a for a in eng.agents if a.desk == "bears")
    t1 = bull.account.rebalance(1.0, 100.0, T - 7200, "тест лонг")
    t2 = bear.account.rebalance(-1.0, 100.0, T - 3600, "тест шорт")
    eng._after_trade(bull, t1, 100.0, 0.01); eng._after_trade(bear, t2, 100.0, 0.01)
    eng.last_price = 110.0
    pos = eng.open_positions(T)
    assert {p["agent"] for p in pos} == {bull.name, bear.name}
    pl = next(p for p in pos if p["agent"] == bull.name); ps = next(p for p in pos if p["agent"] == bear.name)
    assert pl["side"] == "long" and pl["upnl"] > 0 and abs(pl["hours"] - 2.0) < 0.01 and pl["stop"] and pl["stop"] < 110
    assert ps["side"] == "short" and ps["upnl"] < 0 and ps["stop"] > 100 and ps["stop_pct"] is not None
    assert pos[0]["agent"] == bull.name       # сортировка по результату
    assert "positions" in eng.state()
