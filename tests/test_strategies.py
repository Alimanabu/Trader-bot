import pytest

from trader.agents.registry import STRATEGY_FAMILIES, build_strategy
from trader.agents.community import COMMUNITY_STRATEGIES
from trader.agents.rules import RULE_STRATEGIES
from trader.models import Signal


@pytest.mark.parametrize("cls", RULE_STRATEGIES + COMMUNITY_STRATEGIES)
def test_rule_strategies_return_valid_signal(cls, candles):
    s = cls()
    sig = s.decide(candles, {"exposure": 0.0})
    assert isinstance(sig, Signal)
    assert 0.0 <= sig.target_exposure <= 1.0
    assert sig.reason


@pytest.mark.parametrize("cls", RULE_STRATEGIES + COMMUNITY_STRATEGIES)
def test_rule_strategies_hold_when_not_enough_data(cls, candles):
    sig = cls().decide(candles[:5], {"exposure": 0.3})
    assert 0.0 <= sig.target_exposure <= 1.0


def test_llm_strategy_holds_without_key(candles):
    s = build_strategy("llm_technician", None, None)
    sig = s.decide(candles, {"exposure": 0.4})
    assert sig.target_exposure == 0.4
    assert "LLM" in sig.reason


def test_registry_has_all_families():
    assert len(STRATEGY_FAMILIES) == 27


def test_llm_budget_blocks_calls():
    from trader.journal import Journal
    from trader.llm import ClaudeClient, LLMUnavailable, estimate_cost
    import pytest as _pt

    class FakeUsage:
        input_tokens = 1_000_000; output_tokens = 0; cache_creation_input_tokens = 0; cache_read_input_tokens = 0; server_tool_use = None
    assert abs(estimate_cost("claude-sonnet-5", FakeUsage()) - 2.0) < 1e-9
    j = Journal(":memory:")
    c = ClaudeClient(None, "claude-sonnet-5", daily_budget_usd=1.0, spend_store=j)
    c._load_spend(); c._spend_usd = 1.5
    j.kv_set(f"llm_spend:{c._today()}", {"usd": 1.5, "calls": 3})
    c2 = ClaudeClient(None, "claude-sonnet-5", daily_budget_usd=1.0, spend_store=j)
    assert c2.spend_today()["usd"] == 1.5 and c2.spend_today()["calls"] == 3
    with _pt.raises(LLMUnavailable):
        c2._check_budget()


def test_sided_wrapper_maps_signals(candles):
    from trader.agents.registry import build_strategy
    from trader.models import Action
    bear = build_strategy("sma_cross_short")
    both = build_strategy("sma_cross_both")
    base = build_strategy("sma_cross")
    b = base.decide(candles, {"exposure": 0.0})
    s1 = bear.decide(candles, {"exposure": 0.0})
    s2 = both.decide(candles, {"exposure": 0.0})
    if b.action == Action.SELL:
        assert s1.target_exposure == -1.0 and s2.target_exposure == -1.0
    elif b.action == Action.BUY:
        assert s1.target_exposure == 0.0 and s2.target_exposure > 0
    assert bear.side == "short" and both.side == "both" and bear.family == "sma_cross_short"


def test_backtest_runs_for_sided_families(candles):
    from trader.research import backtest
    from trader.agents.registry import build_strategy
    r = backtest(build_strategy("supertrend_short"), candles[-300:])
    assert r.bars > 0 and r.family == "supertrend_short"


@pytest.mark.parametrize("cls", COMMUNITY_STRATEGIES)
def test_community_strategies_trade_and_survive_research(cls, candles):
    """Каждая новая семья хотя бы раз входит и выходит на синтетике, проходит бэктест и перебор параметров."""
    from trader.models import Action
    from trader.research import StrategyLab, walk_forward
    s = cls()
    actions = set()
    for i in range(s.warmup() + 5, len(candles)):
        sig = s.decide(candles[max(0, i - 300):i], {"exposure": 0.0})
        actions.add(sig.action)
    assert Action.BUY in actions or Action.SELL in actions
    r = walk_forward(s, candles)
    assert r.bars > 0 and r.family == cls.family
    best = StrategyLab(max_combos=3).best_params(cls.family, candles[-300:])
    assert best.family == cls.family
    for side in ("short", "both"):
        from trader.agents.registry import build_strategy
        st = build_strategy(f"{cls.family}_{side}")
        assert st.side == side and -1.0 <= st.decide(candles, {"exposure": 0.0}).target_exposure <= 1.0
