from trader.data import indicators as ind


def test_sma_basic():
    assert ind.sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_ema_starts_from_sma():
    e = ind.ema([1, 2, 3, 4, 5, 6], 3)
    assert e[:2] == [None, None]
    assert e[2] == 2.0
    assert e[-1] > e[-2]


def test_rsi_range():
    vals = [100 + (i % 7) * 3 - (i % 3) for i in range(60)]
    r = ind.rsi(vals, 14)
    assert all(0 <= x <= 100 for x in r if x is not None)
    assert r[14] is not None and r[13] is None


def test_bollinger_order():
    vals = [float(i) for i in range(50)]
    lo, mid, up = ind.bollinger(vals, 20, 2.0)
    assert lo[-1] < mid[-1] < up[-1]


def test_macd_lengths():
    vals = [100 + i * 0.5 for i in range(80)]
    line, sig, hist = ind.macd(vals)
    assert len(line) == len(sig) == len(hist) == 80
    assert hist[-1] is not None


def test_donchian():
    lo, up = ind.donchian([5, 6, 7, 8], [1, 2, 3, 4], 2)
    assert up[-1] == 8 and lo[-1] == 3
