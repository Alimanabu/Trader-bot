from trader.paper import PaperAccount


def test_buy_then_sell_with_fees():
    acc = PaperAccount("a", cash=1000, fee_rate=0.001, slippage_rate=0.0)
    t = acc.rebalance(1.0, 100.0, 1)
    assert t and t.side == "BUY"
    assert acc.cash < 1.0
    assert abs(acc.equity(100.0) - 1000 * (1 - 0.001)) < 0.01
    t2 = acc.rebalance(0.0, 110.0, 2)
    assert t2 and t2.side == "SELL"
    assert acc.btc == 0.0
    assert acc.equity(110.0) > 1000
    assert t2.pnl is not None and t2.pnl > 0 and t2.cost is not None
    assert abs(t2.pnl - (acc.equity(110.0) - 1000)) < 1e-6   # итог продажи = вся прибыль круга


def test_no_leverage():
    acc = PaperAccount("a", cash=1000, fee_rate=0.0, slippage_rate=0.0)
    acc.rebalance(5.0, 100.0, 1)
    assert acc.btc * 100 <= 1000.0 + 1e-9
    assert acc.cash >= -1e-9


def test_small_rebalance_ignored():
    acc = PaperAccount("a", cash=1000, fee_rate=0.0, slippage_rate=0.0)
    acc.rebalance(0.5, 100.0, 1)
    n = len(acc.trades)
    acc.rebalance(0.52, 100.0, 2)
    assert len(acc.trades) == n


def test_flatten():
    acc = PaperAccount("a", cash=1000, fee_rate=0.0, slippage_rate=0.0)
    acc.rebalance(0.7, 100.0, 1)
    acc.flatten(100.0, 2)
    assert acc.btc == 0.0
    assert abs(acc.cash - 1000) < 1e-6


def test_short_round_trip_and_flip():
    acc = PaperAccount("s", cash=1000, fee_rate=0.001, slippage_rate=0.0, allow_short=True)
    t = acc.rebalance(-1.0, 100.0, 1)
    assert t.side == "SELL" and acc.btc < 0 and t.pos_after < 0 and t.pnl is None
    assert acc.equity(90.0) > 1000            # шорт зарабатывает на падении
    t2 = acc.rebalance(0.0, 90.0, 2)
    assert t2.side == "BUY" and t2.pnl is not None and t2.pnl > 0 and abs(acc.btc) < 1e-9
    assert abs(t2.pnl - (acc.equity(90.0) - 1000)) < 1e-6
    # разворот из шорта в лонг одной сделкой
    acc.rebalance(-1.0, 100.0, 3)
    t3 = acc.rebalance(1.0, 100.0, 4)
    assert t3.side == "BUY" and acc.btc > 0 and t3.pnl is not None
    # без плеча: модуль позиции не больше капитала
    assert abs(acc.exposure(100.0)) <= 1.01


def test_spot_account_cannot_short():
    acc = PaperAccount("l", cash=1000, fee_rate=0.0, slippage_rate=0.0)
    assert acc.rebalance(-1.0, 100.0, 1) is None and acc.btc == 0


def test_funding_only_for_futures_accounts():
    f = PaperAccount("f", cash=1000, allow_short=True); f.rebalance(-1.0, 100.0, 1)
    assert f.apply_funding(100.0, 8.0) > 0
    l = PaperAccount("l", cash=1000); l.rebalance(1.0, 100.0, 1)
    assert l.apply_funding(100.0, 8.0) == 0.0
