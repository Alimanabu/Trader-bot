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


def test_registry_has_ten_families():
    assert len(STRATEGY_FAMILIES) == 10
