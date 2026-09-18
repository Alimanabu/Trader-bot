"""Терминал: капитал по результатам, стресс-тест, тепловые карты, похожесть, машина времени,
сигналы, брифинг, push, зеркало реального счёта, лаборатория."""
import json

from tests.test_engine import make_engine, hours
from trader.journal import Journal
from trader.models import Candle


def _fresh(settings, interns=0):
    settings.intern_count = interns
    eng, market = make_engine(settings)
    last = market.candles("BTCUSDT", "1h", 1)[-1]
    T = last.ts + 3600 + 5
    eng.tick(now=T)
    return eng, market, T


def test_capital_flows_to_winners(settings):
    eng, market, T = _fresh(settings)
    team = [a for a in eng.agents if a.status == "active"]
    for a in team:
        a.hired_at = T - 10 * 86400
        a.last_ts_seen = T
    star, weak = team[0], team[1]
    star.streak_weeks = 2; star.account.cash += 30          # третья неделя в плюсе
    weak.account.cash -= 30                                  # минус
    total_before = sum(a.equity(eng.last_price) for a in team)
    res = eng.tick(now=T + 7 * 86400)
    changes = res["weekly"]["capital"]
    assert changes, "ожидалось перераспределение"
    assert star.start_balance() > settings.agent_start_balance * 1.3
    team_after = [a for a in eng.agents if a.status == "active"]
    assert any(a.start_balance() < settings.agent_start_balance for a in team_after)     # слабые отдали капитал
    assert abs(sum(c["to"] - c["from"] for c in changes)) < 1.0                          # переводы в сумме дают ноль
    assert any(e["kind"] == "capital" for e in eng.j.recent_events(60))
    # история результата не искажена: прибыль звезды по-прежнему считается от вложенного
    assert abs(star.pnl_total(eng.last_price) - 30) < 5


def test_stress_test_and_heatmap_and_snapshot(settings):
    eng, market, T = _fresh(settings)
    for a in eng.agents:
        a.account.flatten(eng.last_price, T, "тест")
    bull = next(a for a in eng.agents if a.desk == "bulls")
    t = bull.account.rebalance(1.0, eng.last_price, T, "тест")
    eng.j.trade(t)
    eng._after_trade(bull, t, eng.last_price, 0.01)
    st = eng.stress_test()
    assert st["moves"]["-5"]["pnl"] < 0 and st["at_risk_pct"] > 0 and st["unprotected"] == 0
    # убыток ограничен стопом: при −5% теряем не больше, чем до стопа (2% + проскальзывание)
    assert abs(st["moves"]["-5"]["pnl"]) <= bull.account.btc * eng.last_price * 0.03
    hm = eng.j.heatmap({a.name for a in eng.agents}, 0)
    assert len(hm["hours"]) == 24 and len(hm["weekdays"]) == 7
    snap = eng.snapshot_at(T + 10)
    assert snap["price"] > 0 and any(p["agent"] == bull.name and p["side"] == "long" for p in snap["positions"])
    assert "decisions" in snap and "views" in snap


def test_correlation_matrix(settings):
    settings.intern_count = 0
    eng, market = make_engine(settings)
    hours(eng, market, 40)
    c = eng.correlation(days=3650)
    assert len(c["names"]) >= 2 and len(c["matrix"]) == len(c["names"])
    assert all(abs(c["matrix"][i][i] - 1.0) < 1e-6 or c["matrix"][i][i] == 0.0 for i in range(len(c["names"])))
    assert c["pairs"] and -1.0 <= c["pairs"][0]["r"] <= 1.0


def test_alerts_fire_once_and_repeat(settings):
    eng, market, T = _fresh(settings)
    price = eng.last_price
    a1 = eng.j.alert_add("price_above", price * 0.9, ts=T)
    a2 = eng.j.alert_add("company_day_below", 50, ts=T)            # не сработает
    bull = next(a for a in eng.agents if a.desk == "bulls")
    a3 = eng.j.alert_add("agent_entry", 0, bull.name, repeat=True, ts=T)
    res = eng.tick(now=T + 60)
    fired = {f["id"] for f in res["alerts"]}
    assert a1 in fired and a2 not in fired
    assert not [x for x in eng.j.alerts(active_only=True) if x["id"] == a1]      # одноразовый выключился
    assert any(e["kind"] == "alert" for e in eng.j.recent_events(20))
    bull.account.flatten(price, T + 100, "тест")
    bull.account.rebalance(1.0, price, T + 120, "тест вход")
    res = eng.tick(now=T + 180)
    assert a3 in {f["id"] for f in res["alerts"]}
    assert [x for x in eng.j.alerts(active_only=True) if x["id"] == a3]         # повторяющийся остался


def test_morning_briefing_without_llm(settings):
    eng, market, T = _fresh(settings)
    b = eng.morning_briefing(T + 100)
    assert b and "сделок" in b["text"]
    assert eng.j.knowledge("briefing", "active")[0]["id"] == b["id"]
    assert eng.state()["briefing"]["text"] == b["text"]


def test_push_subscriptions_and_delivery(settings):
    eng, market, T = _fresh(settings)
    sent = []
    eng.push.send = lambda sub, payload, ttl=3600: (sent.append((sub["endpoint"], payload)) or 201)
    eng.j.push_add({"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}}, "телефон")
    assert eng.state()["push"]["subscribers"] == 1
    eng._push_new_events(T)                        # первая инициализация: ничего не шлём, запоминаем последнее событие
    eng.j.event("stop", "Тест: сработал стоп-лосс", "X", ts=T + 1)
    eng.j.event("lesson", "не должно уйти", "X", ts=T + 2)
    eng._send_push(eng.j.push_subs(), [{"title": "t", "body": "b"}])
    assert sent and sent[0][0] == "https://push.example/abc"
    sent.clear()
    eng._push_new_events(T + 5)
    import time as _t
    for _ in range(50):
        if sent:
            break
        _t.sleep(0.02)
    assert sent and "стоп" in sent[0][1]["body"].lower()
    # мёртвая подписка удаляется
    eng.push.send = lambda sub, payload, ttl=3600: 410
    eng._send_push(eng.j.push_subs(), [{"title": "t", "body": "b"}])
    assert eng.j.push_subs() == []
    # шифрование и подпись на настоящих ключах
    from trader.push import VapidKeys, encrypt, _b64u
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    import os
    ua = ec.generate_private_key(ec.SECP256R1())
    pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    body = encrypt(b'{"a":1}', _b64u(pub), _b64u(os.urandom(16)))
    assert len(body) == 16 + 4 + 1 + 65 + len(b'{"a":1}') + 1 + 16
    assert eng.push.keys.jwt("https://fcm.googleapis.com", "mailto:x@y").count(".") == 2


class FakeHttp:
    def __init__(self):
        self.calls = []

    def request(self, method, url, headers=None):
        self.calls.append((method, url, headers))

        class R:
            status_code = 200
            text = ""

            def json(self_inner):
                if "/order" in url:
                    return {"orderId": 7, "executedQty": "0.00100", "cummulativeQuoteQty": "50.00", "status": "FILLED"}
                return {"balances": [{"asset": "USDT", "free": "1000.0"}, {"asset": "BTC", "free": "0"}]}
        return R()


def test_broker_signs_and_mirrors_live_trades(settings):
    from trader.broker import BinanceSpotBroker
    http = FakeHttp()
    b = BinanceSpotBroker("key", "secret", testnet=True, http=http)
    r = b.market_buy("BTCUSDT", 50.0)
    assert r["qty"] == 0.001 and r["price"] == 50000.0 and "signature=" in http.calls[-1][1] and http.calls[-1][2]["X-MBX-APIKEY"] == "key"
    assert b.balances()["USDT"] == 1000.0
    settings.live_enabled = True
    settings.live_api_key, settings.live_api_secret = "k", "s"
    eng, market, T = _fresh(settings)
    eng.broker._http = http
    bull = next(a for a in eng.agents if a.desk == "bulls")
    bull.rank = 3
    bull.account.flatten(eng.last_price, T, "тест")
    from trader.models import Action, Signal
    bull.strategy.decide = lambda candles, ctx=None: Signal(Action.BUY, 1.0, 1.0, "тест")
    bull.next_check_ts = 0
    eng.tick(now=T + 60)
    orders = eng.j.live_orders()
    assert orders and orders[0]["side"] == "BUY" and orders[0]["testnet"] == 1 and orders[0]["agent"] == bull.name
    assert eng.live_pos[bull.name] > 0 and any(e["kind"] == "live" for e in eng.j.recent_events(20))
    st = eng.live_state()
    assert st["enabled"] and st["testnet"] and bull.name in st["agents"]


def test_lab_backtest_returns_curve(settings):
    eng, market, T = _fresh(settings)
    r = eng.lab_backtest("sma_cross", {"fast": 10, "slow": 30}, days=10)
    assert r["curve"] and r["result"]["family"] == "sma_cross" and r["params"]["fast"] == 10
    r2 = eng.lab_backtest("sma_cross_short", None, days=10)
    assert r2["result"]["family"] == "sma_cross_short"


def test_macro_describe():
    from trader.data.macro import describe
    assert "недоступны" in describe(None)
    assert "страх" in describe({"fng": 20, "fng_label": "крайний страх", "open_interest": 90000.0, "oi_change_pct": 1.5})
