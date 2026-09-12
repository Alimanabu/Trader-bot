"""Источники котировок. Ключи биржи не нужны: используются публичные эндпоинты."""
from __future__ import annotations

import logging
import math
import random
import time
from abc import ABC, abstractmethod

import httpx

from ..models import Candle

log = logging.getLogger(__name__)

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


class MarketData(ABC):
    name = "abstract"

    @abstractmethod
    def candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Последние `limit` закрытых свечей, от старых к новым."""


class BinanceMarket(MarketData):
    name = "binance"
    base_url = "https://api.binance.com"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=20)

    def candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        r = self.client.get(
            f"{self.base_url}/api/v3/klines",
            params={"symbol": symbol, "interval": timeframe, "limit": min(limit + 1, 1000)},
        )
        r.raise_for_status()
        rows = r.json()
        out = [Candle(int(k[0]) // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in rows]
        return _drop_open_candle(out, timeframe)


class BybitMarket(MarketData):
    name = "bybit"
    base_url = "https://api.bybit.com"
    _tf = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=20)

    def candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        r = self.client.get(
            f"{self.base_url}/v5/market/kline",
            params={"category": "spot", "symbol": symbol, "interval": self._tf[timeframe], "limit": min(limit + 1, 1000)},
        )
        r.raise_for_status()
        rows = r.json()["result"]["list"]
        out = [Candle(int(k[0]) // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in rows]
        out.sort(key=lambda c: c.ts)
        return _drop_open_candle(out, timeframe)


class FallbackMarket(MarketData):
    """Пробует источники по очереди: если Binance недоступен, берёт Bybit."""
    name = "fallback"

    def __init__(self, sources: list[MarketData]):
        self.sources = sources

    def candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        last_err: Exception | None = None
        for src in self.sources:
            try:
                data = src.candles(symbol, timeframe, limit)
                if data:
                    self.name = src.name
                    return data
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("Источник %s недоступен: %s", src.name, e)
        raise RuntimeError(f"Ни один источник котировок не ответил: {last_err}")


class SyntheticMarket(MarketData):
    """Синтетический рынок для тестов и работы без интернета.

    Генерирует правдоподобный ряд цен (геометрическое случайное блуждание с режимами),
    детерминированный при фиксированном seed.
    """
    name = "synthetic"

    def __init__(self, seed: int = 42, start_price: float = 60000.0, start_ts: int | None = None):
        self.seed = seed
        self.start_price = start_price
        self.start_ts = start_ts or (int(time.time()) // 3600 * 3600 - 3600 * 5000)
        self._cache: list[Candle] = []
        self._offset = 0

    def _generate(self, n: int, timeframe: str) -> list[Candle]:
        rng = random.Random(self.seed)
        step = TIMEFRAME_SECONDS[timeframe]
        price = self.start_price
        out: list[Candle] = []
        drift = 0.0
        vol = 0.006
        for i in range(n):
            if i % 120 == 0:
                drift = rng.choice([-0.0008, -0.0003, 0.0, 0.0003, 0.0008])
                vol = rng.choice([0.004, 0.006, 0.009])
            r = rng.gauss(drift, vol)
            o = price
            c = price * math.exp(r)
            h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
            l = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
            v = abs(rng.gauss(500, 200)) * (1 + 5 * abs(r) / vol)
            out.append(Candle(self.start_ts + i * step, o, h, l, c, v))
            price = c
        return out

    def advance(self, n: int = 1) -> None:
        """Сдвинуть «текущее время» вперёд на n свечей (для симуляции)."""
        self._offset += n

    def candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        total = limit + self._offset + 1
        if len(self._cache) < total:
            self._cache = self._generate(max(total, 6000), timeframe)
        end = len(self._cache) - 1 - max(0, (len(self._cache) - 1 - limit - self._offset))
        end = min(len(self._cache), limit + self._offset)
        return self._cache[max(0, end - limit):end]


def _drop_open_candle(candles: list[Candle], timeframe: str) -> list[Candle]:
    """Биржи возвращают последнюю, ещё не закрытую свечу. Убираем её."""
    if not candles:
        return candles
    step = TIMEFRAME_SECONDS.get(timeframe, 3600)
    now = int(time.time())
    if candles[-1].ts + step > now:
        return candles[:-1]
    return candles


def get_market(source: str) -> MarketData:
    source = (source or "binance").lower()
    if source == "synthetic":
        return SyntheticMarket()
    if source == "bybit":
        return FallbackMarket([BybitMarket(), BinanceMarket()])
    return FallbackMarket([BinanceMarket(), BybitMarket()])
