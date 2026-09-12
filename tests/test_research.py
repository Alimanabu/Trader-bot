from trader.agents.rules import SmaCross
from trader.research import StrategyLab, backtest, grid


def test_backtest_runs(candles):
    r = backtest(SmaCross(), candles)
    assert r.bars > 0
    assert r.max_drawdown_pct >= 0


def test_grid_valid_combos():
    for p in grid("sma_cross", max_combos=100):
        assert p["fast"] < p["slow"]


def test_lab_returns_ranked(candles):
    lab = StrategyLab(max_combos=3)
    res = lab.research(candles[-200:], families=["sma_cross", "rsi_reversion"], top_n=3)
    assert len(res) == 3
    assert res[0].score() >= res[1].score() >= res[2].score()
