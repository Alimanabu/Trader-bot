"""Отдел внешних сигналов: разбор текста, проверка ценой, рейтинг источников, стажёр-последователь."""
import pytest

from tests.test_engine import make_engine
from trader.agents.registry import STRATEGY_FAMILIES, all_families
from trader.signals import SignalDesk, parse_signal, is_follower
from trader.journal import Journal


def test_parse_labeled_english_and_russian():
    p = parse_signal("BTC/USDT LONG\nEntry: 80500-80700\nSL: 79800\nTP1: 82000 TP2: 84000")
    assert p["side"] == "long" and p["entry"] == 80600 and p["stop"] == 79800 and p["target"] == 82000 and p["targets"] == [82000, 84000]
    p = parse_signal("Шорт по битку, вход 81 200, стоп 82 000, цели 80 000 и 79 500")
    assert p["side"] == "short" and p["entry"] == 81200 and p["stop"] == 82000 and p["target"] == 80000
    p = parse_signal("buy 80.5k sl 79k tp 83k")
    assert p["side"] == "long" and p["entry"] == 80500 and p["stop"] == 79000 and p["target"] == 83000


def test_parse_without_labels_orders_levels_by_side():
    p = parse_signal("лонг 80500 79800 82000")
    assert p["entry"] == 80500 and p["stop"] == 79800 and p["target"] == 82000
    p = parse_signal("short 81000 82000 79000")
    assert p["entry"] == 81000 and p["stop"] == 82000 and p["target"] == 79000
    p = parse_signal("вход 80500 стоп 79800")            # направление выводится из положения стопа
    assert p["side"] == "long"
    assert parse_signal("просто текст без чисел")["entry"] is None


def _desk(settings):
    j = Journal(":memory:")
    return SignalDesk(settings, j), j


def test_add_validates_levels_and_market_entry(settings):
    desk, j = _desk(settings)
    with pytest.raises(ValueError):
        desk.add(1000, "Канал А", "long", 80000, 81000, 82000, "", 80000)      # стоп выше входа
    with pytest.raises(ValueError):
        desk.add(1000, "", "long", 80000, 79000, 82000, "", 80000)
    s = desk.add(1000, "Канал А", "long", None, 79000, 82000, "", 80000)       # без цены входа → по рынку
    assert s["status"] == "open" and s["entry"] == 80000
    s = desk.add(1000, "Канал А", "long", 78000, 77000, 82000, "", 80000)      # вход далеко ниже рынка → ждёт
    assert s["status"] == "pending"
    assert any(e["kind"] == "signal" for e in j.recent_events(5))


def test_evaluate_hit_stop_expire_and_pending(settings):
    desk, j = _desk(settings)
    T = 100_000
    a = desk.add(T, "Канал А", "long", None, 79000, 82000, "", 80000)
    b = desk.add(T, "Канал А", "short", None, 81000, 78000, "", 80000)
    c = desk.add(T, "Канал Б", "long", None, 0, 0, "", 80000)             # без стопа и цели: закроется по времени
    d = desk.add(T, "Канал Б", "long", 78000, 77000, 81000, "", 80000)    # ждёт входа
    done = desk.evaluate(82500, T + 600, hi=82500, lo=80000)
    st = {x["id"]: x["status"] for x in done}
    assert st[a["id"]] == "hit" and st[b["id"]] == "stopped"
    hit = j.signal_by_id(a["id"])
    assert hit["result_pct"] == pytest.approx((82000 / 80000 - 1) * 100 - desk.fees_pct(), abs=1e-6)
    assert j.signal_by_id(b["id"])["result_pct"] < 0
    # ожидающий сигнал входит, когда цена дошла до входа
    desk.evaluate(77900, T + 1200, hi=78100, lo=77900)
    assert j.signal_by_id(d["id"])["status"] == "open"
    # сигнал без стопа и цели закрывается по времени с итогом по рынку
    desk.evaluate(80800, T + settings.signal_max_h * 3600 + 1, hi=80800, lo=80000)
    row = j.signal_by_id(c["id"])
    assert row["status"] == "expired" and row["result_pct"] == pytest.approx(1.0 - desk.fees_pct(), abs=1e-6)
    # ожидающий сигнал, до которого цена не дошла, отменяется
    e = desk.add(T, "Канал В", "short", 90000, 91000, 85000, "", 80000)
    desk.evaluate(80000, T + settings.signal_entry_h * 3600 + 1)
    assert j.signal_by_id(e["id"])["status"] == "cancelled"
    src = {x["source"]: x for x in desk.sources()}
    assert src["Канал А"]["scored"] == 2 and src["Канал А"]["wins"] == 1 and src["Канал А"]["rating"] == "checking"
    assert "Канал В" in src and src["Канал В"]["scored"] == 0


def _seed_source(j, source, n_win, n_loss, ts):
    for i in range(n_win):
        sid = j.signal_add(ts - 1000 - i, source, "long", 80000, 79000, 82000, "", 80000, "open")
        j.signal_close(sid, "hit", 2.0, ts - 500, 82000)
    for i in range(n_loss):
        sid = j.signal_add(ts - 1000 - i, source, "long", 80000, 79000, 82000, "", 80000, "open")
        j.signal_close(sid, "stopped", -1.5, ts - 500, 79000)


def test_proven_source_gets_follower_that_trades_by_signal(settings):
    settings.intern_count = 2
    settings.signal_min_count = 10
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    price = eng.last_price
    _seed_source(eng.j, "Канал А", 7, 3, T)       # 70% точности, в плюсе → кандидат
    _seed_source(eng.j, "Слив", 3, 7, T)          # 30% → не прошёл
    res = eng.tick(now=T + 60)
    followers = [a for a in eng.agents if is_follower(a)]
    assert len(followers) == 1 and followers[0].strategy.params["source"] == "Канал А" and followers[0].status == "intern"
    assert "Сигналы: Канал А" in res["interns_added"]
    src = {x["source"]: x for x in eng.signals.sources(eng.agents)}
    assert src["Канал А"]["rating"] == "candidate" and src["Канал А"]["follower"]["name"] == "Сигналы: Канал А"
    assert src["Слив"]["rating"] == "failed" and src["Слив"]["follower"] is None
    f = followers[0]
    # штатных стажёров по-прежнему intern_count: последователь сверх штата
    assert len([a for a in eng.agents if a.status == "intern" and not is_follower(a)]) == settings.intern_count
    # сигнала нет: стоит в деньгах
    eng.tick(now=T + 120, force=True)
    assert abs(f.account.btc) < 1e-12
    # пришёл шорт-сигнал: следует за ним, стоп берёт из сигнала
    s = eng.signals.add(T + 130, "Канал А", "short", None, price * 1.02, price * 0.97, "", price)
    eng.tick(now=T + 180, force=True)
    assert f.account.btc < 0 and f.stop_price == pytest.approx(price * 1.02, rel=1e-6)
    assert "Канал А" in (f.last_signal.reason or "")
    # цель достигнута → сигнал закрыт → последователь выходит
    market.advance(0)
    eng.signals.evaluate(price * 0.96, T + 240, hi=price, lo=price * 0.96)
    assert eng.j.signal_by_id(s["id"])["status"] == "hit"
    eng.tick(now=T + 300, force=True)
    assert abs(f.account.btc) < 1e-12
    # последователя не отчисляют за простой и не переобучают
    eng.director.drop_idle_interns(eng.agents, price, T + 10 * 86400)
    assert f.status == "intern"
    assert not eng.learner.retune([f], eng.last_candles, T + 400)
    assert f.strategy.params["source"] == "Канал А"
    st = eng.state()
    assert st["signals"]["sources"] and any(x["rating"] == "candidate" for x in st["signals"]["sources"])


def test_failed_source_loses_follower(settings):
    settings.intern_count = 1
    settings.signal_min_count = 10
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    _seed_source(eng.j, "Канал А", 7, 3, T)
    eng.tick(now=T + 60)
    f = next(a for a in eng.agents if is_follower(a))
    _seed_source(eng.j, "Канал А", 0, 10, T + 100)      # серия стопов: точность падает до 35%
    eng.tick(now=T + 120)
    assert f.status == "dropped"
    eng.tick(now=T + 180)
    assert len([a for a in eng.agents if is_follower(a) and a.status == "intern"]) == 0   # две недели не пересоздаём


def test_follower_family_is_registered_but_not_researched():
    assert "signal_follower" in STRATEGY_FAMILIES
    assert "signal_follower" not in all_families()
