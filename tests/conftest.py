import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from trader.config import Settings
from trader.data.market import SyntheticMarket


@pytest.fixture
def settings():
    return Settings(market_source="synthetic", db_path=":memory:", history_candles=400, research_lookback=300)


@pytest.fixture
def candles():
    return SyntheticMarket(seed=1).candles("BTCUSDT", "1h", 400)
