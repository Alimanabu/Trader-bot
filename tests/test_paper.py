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
