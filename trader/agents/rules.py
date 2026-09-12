"""Восемь стратегий на правилах. У каждой своя «теория рынка»."""
from __future__ import annotations

from ..data import indicators as ind
from ..models import Action, Candle, Signal
from .base import Strategy, hold


def _closes(c: list[Candle]) -> list[float]:
    return [x.close for x in c]


class SmaCross(Strategy):
    family = "sma_cross"
    description = "Тренд: быстрая SMA выше медленной — покупаем, ниже — выходим."

    @classmethod
    def default_params(cls):
        return {"fast": 20, "slow": 50}

    @classmethod
    def param_grid(cls):
        return {"fast": [10, 20, 30], "slow": [50, 100, 200]}

    def cadence_minutes(self):
        return 10

    def warmup(self):
        return self.params["slow"] + 2

    def decide(self, candles, context=None):
        closes = _closes(candles)
        f = ind.sma(closes, self.params["fast"])
        s = ind.sma(closes, self.params["slow"])
        if f[-1] is None or s[-1] is None:
            return hold("недостаточно данных")
        gap = (f[-1] - s[-1]) / s[-1]
        if f[-1] > s[-1]:
            conf = min(1.0, abs(gap) * 50)
            return Signal(Action.BUY, 1.0, conf, f"SMA{self.params['fast']} выше SMA{self.params['slow']} на {gap*100:.2f}%")
        return Signal(Action.SELL, 0.0, min(1.0, abs(gap) * 50), f"SMA{self.params['fast']} ниже SMA{self.params['slow']}")


class EmaMomentum(Strategy):
    family = "ema_momentum"
    description = "Импульс: цена выше EMA и EMA растёт — держим, иначе выходим."

    @classmethod
    def default_params(cls):
        return {"period": 34, "slope_lookback": 5}

    @classmethod
    def param_grid(cls):
        return {"period": [21, 34, 55, 89], "slope_lookback": [3, 5, 8]}

    def cadence_minutes(self):
        return 5

    def warmup(self):
        return self.params["period"] + self.params["slope_lookback"] + 2

    def decide(self, candles, context=None):
        closes = _closes(candles)
        e = ind.ema(closes, self.params["period"])
        lb = self.params["slope_lookback"]
        if e[-1] is None or e[-1 - lb] is None:
            return hold("недостаточно данных")
        rising = e[-1] > e[-1 - lb]
        above = closes[-1] > e[-1]
        if rising and above:
            return Signal(Action.BUY, 1.0, 0.7, f"цена выше EMA{self.params['period']}, EMA растёт")
        if not rising and not above:
            return Signal(Action.SELL, 0.0, 0.7, f"цена ниже EMA{self.params['period']}, EMA падает")
        return Signal(Action.HOLD, 0.5, 0.3, "смешанные сигналы, половина позиции")


class RsiReversion(Strategy):
    family = "rsi_reversion"
    description = "Возврат к среднему: RSI перепродан — покупаем, перекуплен — продаём."

    @classmethod
    def default_params(cls):
        return {"period": 14, "oversold": 30, "overbought": 70}

    @classmethod
    def param_grid(cls):
        return {"period": [7, 14, 21], "oversold": [20, 25, 30, 35], "overbought": [65, 70, 75, 80]}

    def cadence_minutes(self):
        return 3

    def warmup(self):
        return self.params["period"] * 3

    def decide(self, candles, context=None):
        closes = _closes(candles)
        r = ind.rsi(closes, self.params["period"])
        if r[-1] is None:
            return hold("недостаточно данных")
        v = r[-1]
        if v < self.params["oversold"]:
            return Signal(Action.BUY, 1.0, min(1.0, (self.params["oversold"] - v) / 15 + 0.5), f"RSI={v:.1f} перепродан")
        if v > self.params["overbought"]:
            return Signal(Action.SELL, 0.0, min(1.0, (v - self.params["overbought"]) / 15 + 0.5), f"RSI={v:.1f} перекуплен")
        cur = context.get("exposure", 0.0) if context else 0.0
        return Signal(Action.HOLD, cur, 0.2, f"RSI={v:.1f} в нейтральной зоне, держим как есть")


class BollingerReversion(Strategy):
    family = "bollinger"
    description = "Полосы Боллинджера: касание нижней — покупка, возврат к середине — продажа."

    @classmethod
    def default_params(cls):
        return {"period": 20, "mult": 2.0}

    @classmethod
    def param_grid(cls):
        return {"period": [14, 20, 30], "mult": [1.5, 2.0, 2.5]}

    def cadence_minutes(self):
        return 3

    def warmup(self):
        return self.params["period"] + 2

    def decide(self, candles, context=None):
        closes = _closes(candles)
        lower, mid, upper = ind.bollinger(closes, self.params["period"], self.params["mult"])
        if lower[-1] is None:
            return hold("недостаточно данных")
        c = closes[-1]
        cur = context.get("exposure", 0.0) if context else 0.0
        if c <= lower[-1]:
            return Signal(Action.BUY, 1.0, 0.8, f"цена {c:.0f} у нижней полосы {lower[-1]:.0f}")
        if c >= upper[-1]:
            return Signal(Action.SELL, 0.0, 0.8, f"цена {c:.0f} у верхней полосы {upper[-1]:.0f}")
        if cur > 0 and c >= mid[-1]:
            return Signal(Action.SELL, 0.0, 0.5, f"цена вернулась к середине {mid[-1]:.0f}, фиксируем")
        return Signal(Action.HOLD, cur, 0.2, "внутри полос")


class DonchianBreakout(Strategy):
    family = "breakout"
    description = "Пробой: закрытие выше максимума N свечей — вход, ниже минимума M — выход."

    @classmethod
    def default_params(cls):
        return {"entry": 24, "exit": 12}

    @classmethod
    def param_grid(cls):
        return {"entry": [12, 24, 48, 96], "exit": [6, 12, 24]}

    def cadence_minutes(self):
        return 1

    def warmup(self):
        return max(self.params["entry"], self.params["exit"]) + 3

    def decide(self, candles, context=None):
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = _closes(candles)
        _, up = ind.donchian(highs[:-1], lows[:-1], self.params["entry"])
        lo, _ = ind.donchian(highs[:-1], lows[:-1], self.params["exit"])
        if up[-1] is None or lo[-1] is None:
            return hold("недостаточно данных")
        c = closes[-1]
        cur = context.get("exposure", 0.0) if context else 0.0
        if c > up[-1]:
            return Signal(Action.BUY, 1.0, 0.8, f"пробой максимума {self.params['entry']} свечей ({up[-1]:.0f})")
        if c < lo[-1]:
            return Signal(Action.SELL, 0.0, 0.8, f"пробой минимума {self.params['exit']} свечей ({lo[-1]:.0f})")
        return Signal(Action.HOLD, cur, 0.2, "внутри канала")


class MacdTrend(Strategy):
    family = "macd"
    description = "MACD: гистограмма выше нуля — покупаем, ниже — продаём."

    @classmethod
    def default_params(cls):
        return {"fast": 12, "slow": 26, "signal": 9}

    @classmethod
    def param_grid(cls):
        return {"fast": [8, 12, 16], "slow": [21, 26, 34], "signal": [6, 9, 12]}

    def cadence_minutes(self):
        return 10

    def warmup(self):
        return self.params["slow"] + self.params["signal"] + 5

    def decide(self, candles, context=None):
        closes = _closes(candles)
        _, _, hist = ind.macd(closes, self.params["fast"], self.params["slow"], self.params["signal"])
        if hist[-1] is None or hist[-2] is None:
            return hold("недостаточно данных")
        h = hist[-1]
        if h > 0:
            conf = 0.6 + (0.3 if hist[-2] <= 0 else 0.0)
            return Signal(Action.BUY, 1.0, conf, f"MACD-гистограмма {h:.1f} > 0")
        conf = 0.6 + (0.3 if hist[-2] >= 0 else 0.0)
        return Signal(Action.SELL, 0.0, conf, f"MACD-гистограмма {h:.1f} < 0")


class VolumeSpike(Strategy):
    family = "volume_spike"
    description = "Объём: всплеск объёма с ростом цены — входим на несколько свечей."

    @classmethod
    def default_params(cls):
        return {"period": 24, "mult": 2.0, "hold_bars": 6}

    @classmethod
    def param_grid(cls):
        return {"period": [12, 24, 48], "mult": [1.5, 2.0, 3.0], "hold_bars": [3, 6, 12]}

    def cadence_minutes(self):
        return 2

    def warmup(self):
        return self.params["period"] + self.params["hold_bars"] + 2

    def decide(self, candles, context=None):
        vols = [c.volume for c in candles]
        avg = ind.sma(vols[:-1], self.params["period"])
        if avg[-1] is None or avg[-1] == 0:
            return hold("недостаточно данных")
        last = candles[-1]
        ratio = last.volume / avg[-1]
        up = last.close > last.open
        # Ищем недавний всплеск в пределах hold_bars
        for k in range(1, self.params["hold_bars"] + 1):
            i = len(candles) - k
            if i < self.params["period"] or avg[i - 1] is None:
                break
            c = candles[i]
            if c.volume / avg[i - 1] >= self.params["mult"] and c.close > c.open:
                return Signal(Action.BUY, 1.0, 0.6, f"объём x{c.volume/avg[i-1]:.1f} с ростом {k} свечей назад")
        if ratio >= self.params["mult"] and not up:
            return Signal(Action.SELL, 0.0, 0.7, f"объём x{ratio:.1f} с падением: выходим")
        return Signal(Action.SELL, 0.0, 0.3, "всплеска нет, вне рынка")


class VolatilityRegime(Strategy):
    family = "vol_regime"
    description = "Режим волатильности: низкий ATR и рост — входим, высокий ATR — сокращаем позицию."

    @classmethod
    def default_params(cls):
        return {"atr_period": 14, "trend_period": 48, "target_vol": 0.01}

    @classmethod
    def param_grid(cls):
        return {"atr_period": [7, 14, 21], "trend_period": [24, 48, 96], "target_vol": [0.006, 0.01, 0.015]}

    def cadence_minutes(self):
        return 15

    def warmup(self):
        return max(self.params["atr_period"], self.params["trend_period"]) + 3

    def decide(self, candles, context=None):
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = _closes(candles)
        a = ind.atr(highs, lows, closes, self.params["atr_period"])
        t = ind.sma(closes, self.params["trend_period"])
        if a[-1] is None or t[-1] is None:
            return hold("недостаточно данных")
        vol = a[-1] / closes[-1]
        size = min(1.0, self.params["target_vol"] / vol) if vol > 0 else 1.0
        if closes[-1] > t[-1]:
            return Signal(Action.BUY, size, 0.6, f"тренд вверх, ATR {vol*100:.2f}% → доля {size:.0%}")
        return Signal(Action.SELL, 0.0, 0.6, f"цена ниже SMA{self.params['trend_period']}, вне рынка")


RULE_STRATEGIES: list[type[Strategy]] = [
    SmaCross, EmaMomentum, RsiReversion, BollingerReversion,
    DonchianBreakout, MacdTrend, VolumeSpike, VolatilityRegime,
]


# ---------------------------------------------------------------------------
# Вторая волна семейств: добавлены по запросу расширить отдел исследований.
# ---------------------------------------------------------------------------


class ZScoreReversion(Strategy):
    family = "zscore"
    description = "Z-score: цена на N сигм ниже среднего — покупаем, вернулась к среднему — продаём."

    @classmethod
    def default_params(cls):
        return {"period": 48, "entry_z": -2.0, "exit_z": 0.0}

    @classmethod
    def param_grid(cls):
        return {"period": [24, 48, 96], "entry_z": [-1.5, -2.0, -2.5], "exit_z": [-0.5, 0.0, 0.5]}

    def cadence_minutes(self):
        return 3

    def warmup(self):
        return self.params["period"] + 2

    def decide(self, candles, context=None):
        closes = _closes(candles)
        m = ind.sma(closes, self.params["period"])
        sd = ind.stddev(closes, self.params["period"])
        if m[-1] is None or sd[-1] is None or sd[-1] == 0:
            return hold("недостаточно данных")
        z = (closes[-1] - m[-1]) / sd[-1]
        cur = context.get("exposure", 0.0) if context else 0.0
        if z <= self.params["entry_z"]:
            return Signal(Action.BUY, 1.0, min(1.0, 0.5 + abs(z) / 5), f"z={z:.2f} ниже порога {self.params['entry_z']}")
        if cur > 0 and z >= self.params["exit_z"]:
            return Signal(Action.SELL, 0.0, 0.6, f"z={z:.2f} вернулся к среднему")
        return Signal(Action.HOLD, cur, 0.2, f"z={z:.2f}, ждём")


class RsiDivergence(Strategy):
    family = "rsi_divergence"
    description = "Дивергенция RSI: цена обновила минимум, а RSI нет — покупка; зеркально — продажа."

    @classmethod
    def default_params(cls):
        return {"rsi_period": 14, "lookback": 24, "hold_bars": 12}

    @classmethod
    def param_grid(cls):
        return {"rsi_period": [7, 14], "lookback": [12, 24, 48], "hold_bars": [6, 12, 24]}

    def cadence_minutes(self):
        return 5

    def warmup(self):
        return self.params["rsi_period"] * 3 + self.params["lookback"] * 2

    def decide(self, candles, context=None):
        closes = _closes(candles)
        r = ind.rsi(closes, self.params["rsi_period"])
        lb = self.params["lookback"]
        if len(closes) < 2 * lb + 2 or r[-1] is None or r[-lb - 1] is None:
            return hold("недостаточно данных")
        cur = context.get("exposure", 0.0) if context else 0.0
        # Сравниваем два окна: предыдущее и текущее.
        prev_lo = min(closes[-2 * lb:-lb])
        cur_lo = min(closes[-lb:])
        prev_hi = max(closes[-2 * lb:-lb])
        cur_hi = max(closes[-lb:])
        rp = [x for x in r[-2 * lb:-lb] if x is not None]
        rc = [x for x in r[-lb:] if x is not None]
        if not rp or not rc:
            return hold("недостаточно данных")
        bull = cur_lo < prev_lo and min(rc) > min(rp) and closes[-1] > closes[-2]
        bear = cur_hi > prev_hi and max(rc) < max(rp) and closes[-1] < closes[-2]
        if bull:
            return Signal(Action.BUY, 1.0, 0.7, f"бычья дивергенция: цена {cur_lo:.0f}<{prev_lo:.0f}, RSI {min(rc):.0f}>{min(rp):.0f}")
        if bear and cur > 0:
            return Signal(Action.SELL, 0.0, 0.7, f"медвежья дивергенция: цена {cur_hi:.0f}>{prev_hi:.0f}, RSI {max(rc):.0f}<{max(rp):.0f}")
        if cur > 0:
            # держим ограниченное число баров после входа
            bars = context.get("bars_in_position", 0) if context else 0
            if bars >= self.params["hold_bars"]:
                return Signal(Action.SELL, 0.0, 0.5, f"вышел срок удержания {self.params['hold_bars']} свечей")
        return Signal(Action.HOLD, cur, 0.2, "дивергенции нет")


class SuperTrend(Strategy):
    family = "supertrend"
    description = "Supertrend: трейлинг-линия на ATR; цена выше линии — в позиции, ниже — вне."

    @classmethod
    def default_params(cls):
        return {"atr_period": 10, "mult": 3.0}

    @classmethod
    def param_grid(cls):
        return {"atr_period": [7, 10, 14, 21], "mult": [2.0, 3.0, 4.0]}

    def cadence_minutes(self):
        return 1

    def warmup(self):
        return self.params["atr_period"] * 3

    def decide(self, candles, context=None):
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = _closes(candles)
        a = ind.atr(highs, lows, closes, self.params["atr_period"])
        start = next((i for i, v in enumerate(a) if v is not None), None)
        if start is None or len(closes) - start < 3:
            return hold("недостаточно данных")
        m = self.params["mult"]
        up_band = lo_band = None
        trend = 1
        final_up = final_lo = None
        for i in range(start, len(closes)):
            hl2 = (highs[i] + lows[i]) / 2
            up = hl2 + m * a[i]
            lo = hl2 - m * a[i]
            if final_up is None:
                final_up, final_lo = up, lo
            else:
                final_up = up if up < final_up or closes[i - 1] > final_up else final_up
                final_lo = lo if lo > final_lo or closes[i - 1] < final_lo else final_lo
            if trend == 1 and closes[i] < final_lo:
                trend = -1
            elif trend == -1 and closes[i] > final_up:
                trend = 1
        line = final_lo if trend == 1 else final_up
        if trend == 1:
            return Signal(Action.BUY, 1.0, 0.7, f"Supertrend вверх, линия {line:.0f}")
        return Signal(Action.SELL, 0.0, 0.7, f"Supertrend вниз, линия {line:.0f}")


class KeltnerBreakout(Strategy):
    family = "keltner"
    description = "Канал Кельтнера: закрытие выше EMA+ATR·k — вход, ниже EMA — выход."

    @classmethod
    def default_params(cls):
        return {"ema_period": 20, "atr_period": 14, "mult": 1.5}

    @classmethod
    def param_grid(cls):
        return {"ema_period": [20, 34, 55], "atr_period": [10, 14, 21], "mult": [1.0, 1.5, 2.0]}

    def cadence_minutes(self):
        return 1

    def warmup(self):
        return max(self.params["ema_period"], self.params["atr_period"]) + 3

    def decide(self, candles, context=None):
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = _closes(candles)
        e = ind.ema(closes, self.params["ema_period"])
        a = ind.atr(highs, lows, closes, self.params["atr_period"])
        if e[-1] is None or a[-1] is None:
            return hold("недостаточно данных")
        upper = e[-1] + self.params["mult"] * a[-1]
        cur = context.get("exposure", 0.0) if context else 0.0
        if closes[-1] > upper:
            return Signal(Action.BUY, 1.0, 0.7, f"закрытие {closes[-1]:.0f} выше канала {upper:.0f}")
        if cur > 0 and closes[-1] < e[-1]:
            return Signal(Action.SELL, 0.0, 0.6, f"закрытие ниже EMA{self.params['ema_period']} {e[-1]:.0f}")
        return Signal(Action.HOLD, cur, 0.2, "внутри канала Кельтнера")


class MultiTimeframe(Strategy):
    family = "mtf"
    description = "Два таймфрейма: тренд по 4h-свечам (EMA), вход по 1h-свечам (откат к EMA)."

    @classmethod
    def default_params(cls):
        return {"htf_ema": 21, "ltf_ema": 13, "htf_bars": 4}

    @classmethod
    def param_grid(cls):
        return {"htf_ema": [13, 21, 34], "ltf_ema": [8, 13, 21], "htf_bars": [4, 6]}

    def cadence_minutes(self):
        return 15

    def warmup(self):
        return self.params["htf_ema"] * self.params["htf_bars"] + 10

    def decide(self, candles, context=None):
        n = self.params["htf_bars"]
        # Собираем старшие свечи из младших (по закрытию последней свечи группы).
        htf_closes = [candles[i + n - 1].close for i in range(0, len(candles) - n + 1, n)]
        closes = _closes(candles)
        he = ind.ema(htf_closes, self.params["htf_ema"])
        le = ind.ema(closes, self.params["ltf_ema"])
        if len(he) < 2 or he[-1] is None or he[-2] is None or le[-1] is None:
            return hold("недостаточно данных")
        htf_up = htf_closes[-1] > he[-1] and he[-1] > he[-2]
        cur = context.get("exposure", 0.0) if context else 0.0
        if htf_up and closes[-1] > le[-1]:
            return Signal(Action.BUY, 1.0, 0.7, f"4h-тренд вверх, 1h выше EMA{self.params['ltf_ema']}")
        if not htf_up:
            return Signal(Action.SELL, 0.0, 0.7, "4h-тренд не подтверждён, вне рынка")
        return Signal(Action.HOLD, cur, 0.3, "4h вверх, ждём возврата 1h выше EMA")


class Seasonality(Strategy):
    family = "seasonality"
    description = "Сезонность: держим позицию только в часы суток, которые статистически росли в последние недели."

    @classmethod
    def default_params(cls):
        return {"lookback_days": 21, "min_edge": 0.0003}

    @classmethod
    def param_grid(cls):
        return {"lookback_days": [7, 14, 21], "min_edge": [0.0002, 0.0004, 0.0008]}

    def cadence_minutes(self):
        return 60

    def warmup(self):
        return self.params["lookback_days"] * 24 + 2

    def decide(self, candles, context=None):
        n = self.params["lookback_days"] * 24
        if len(candles) < n + 2:
            return hold("недостаточно данных")
        hist = candles[-n - 1:]
        import datetime as _dt
        buckets: dict[int, list[float]] = {}
        for prev, cur_c in zip(hist[:-1], hist[1:]):
            hour = _dt.datetime.fromtimestamp(cur_c.ts, tz=_dt.timezone.utc).hour
            buckets.setdefault(hour, []).append(cur_c.close / prev.close - 1)
        next_hour = (_dt.datetime.fromtimestamp(candles[-1].ts, tz=_dt.timezone.utc).hour + 1) % 24
        rets = buckets.get(next_hour, [])
        if not rets:
            return hold("нет статистики по часу")
        edge = sum(rets) / len(rets)
        if edge >= self.params["min_edge"]:
            return Signal(Action.BUY, 1.0, min(1.0, edge / self.params["min_edge"] / 3), f"час {next_hour:02d}:00 UTC в среднем {edge*100:+.3f}% за {len(rets)} дн.")
        return Signal(Action.SELL, 0.0, 0.5, f"час {next_hour:02d}:00 UTC в среднем {edge*100:+.3f}%, вне рынка")


RULE_STRATEGIES += [ZScoreReversion, RsiDivergence, SuperTrend, KeltnerBreakout, MultiTimeframe, Seasonality]
