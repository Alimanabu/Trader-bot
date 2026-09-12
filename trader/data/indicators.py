"""Технические индикаторы на чистом Python (без numpy, чтобы легко ставилось на любой сервер)."""
from __future__ import annotations

import math
from typing import Sequence


def sma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def ema(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or not values:
        return out
    k = 2.0 / (period + 1)
    prev: float | None = None
    for i, v in enumerate(values):
        if prev is None:
            if i >= period - 1:
                prev = sum(values[i - period + 1:i + 1]) / period
                out[i] = prev
            continue
        prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period

    def _rsi(g: float, l: float) -> float:
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = d if d > 0 else 0.0
        loss = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi(avg_gain, avg_loss)
    return out


def stddev(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        out[i] = math.sqrt(var)
    return out


def bollinger(values: Sequence[float], period: int = 20, mult: float = 2.0):
    mid = sma(values, period)
    sd = stddev(values, period)
    upper = [None if m is None or s is None else m + mult * s for m, s in zip(mid, sd)]
    lower = [None if m is None or s is None else m - mult * s for m, s in zip(mid, sd)]
    return lower, mid, upper


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef = ema(values, fast)
    es = ema(values, slow)
    line: list[float | None] = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    valid = [x for x in line if x is not None]
    sig_valid = ema(valid, signal)
    sig: list[float | None] = [None] * len(values)
    offset = len(values) - len(valid)
    for i, v in enumerate(sig_valid):
        sig[offset + i] = v
    hist = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> list[float | None]:
    trs: list[float] = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    return sma(trs, period)


def donchian(highs: Sequence[float], lows: Sequence[float], period: int = 20):
    upper: list[float | None] = [None] * len(highs)
    lower: list[float | None] = [None] * len(lows)
    for i in range(period - 1, len(highs)):
        upper[i] = max(highs[i - period + 1:i + 1])
        lower[i] = min(lows[i - period + 1:i + 1])
    return lower, upper


def pct_change(values: Sequence[float], lag: int = 1) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(lag, len(values)):
        prev = values[i - lag]
        out[i] = (values[i] - prev) / prev if prev else None
    return out
