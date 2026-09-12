import pytest

from trader.agents.registry import STRATEGY_FAMILIES, build_strategy
from trader.agents.rules import RULE_STRATEGIES
from trader.models import Signal


@pytest.mark.parametrize("cls", RULE_STRATEGIES)
def test_rule_strategies_return_valid_signal(cls, candles):
    s = cls()
    sig = s.decide(candles, {"exposure": 0.0})
    assert isinstance(sig, Signal)
    assert 0.0 <= sig.target_exposure <= 1.0
    assert sig.reason


@pytest.mark.parametrize("cls", RULE_STRATEGIES)
def test_rule_strategies_hold_when_not_enough_data(cls, candles):
    sig = cls().decide(candles[:5], {"exposure": 0.3})
    assert 0.0 <= sig.target_exposure <= 1.0


def test_llm_strategy_holds_without_key(candles):
    s = build_strategy("llm_technician", None, None)
    sig = s.decide(candles, {"exposure": 0.4})
    assert sig.target_exposure == 0.4
    assert "LLM" in sig.reason


def test_registry_has_all_families():
    assert len(STRATEGY_FAMILIES) == 17


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
