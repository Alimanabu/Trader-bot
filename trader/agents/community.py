"""Стратегии по мотивам открытых библиотек (Freqtrade и подобных), переписанные под интерфейс Botz.

Каждая семья: свой индикаторный «кирпич», вход и выход. Отдел исследований проверяет их на реальной
истории, ротация отсеивает пустые. Для медведей и двусторонних десков есть зеркальные версии.
"""
from __future__ import annotations

from ..data import indicators as ind
from ..models import Action, Candle, Signal
from .base import Strategy, hold


def _closes(c: list[Candle]) -> list[float]:
    return [x.close for x in c]


def _hl(c: list[Candle]):
    return [x.high for x in c], [x.low for x in c]


class BollingerRsi(Strategy):
    """BbandRsi: цена под нижней полосой и RSI перепродан → покупка; RSI перекуплен → выход."""
    family = "bb_rsi"
    description = "Боллинджер + RSI: покупка, когда цена под нижней полосой и RSI перепродан; выход при перекупленности."

    @classmethod
    def default_params(cls):
        return {"period": 20, "mult": 2.0, "rsi": 14, "oversold": 30, "overbought": 70}

    @classmethod
    def param_grid(cls):
        return {"period": [20, 30], "mult": [2.0, 2.5], "oversold": [25, 30, 35], "overbought": [65, 70, 75]}

    def cadence_minutes(self):
        return 5

    def warmup(self):
        return max(self.params["period"], self.params["rsi"]) + 5

    def decide(self, candles, context=None):
        closes = _closes(candles)
        lower, mid, upper = ind.bollinger(closes, self.params["period"], self.params["mult"])
        r = ind.rsi(closes, self.params["rsi"])[-1]
        if lower[-1] is None or r is None:
            return hold("недостаточно данных")
        c = closes[-1]
        if c < lower[-1] and r < self.params["oversold"]:
            return Signal(Action.BUY, 1.0, min(1.0, (self.params["oversold"] - r) / 20 + 0.5), f"цена {c:.0f} под нижней полосой {lower[-1]:.0f}, RSI={r:.0f} перепродан")
        if r > self.params["overbought"]:
            return Signal(Action.SELL, 0.0, 0.7, f"RSI={r:.0f} перекуплен, выходим")
        return hold(f"RSI={r:.0f}, цена внутри полос, ждём", (context or {}).get("exposure", 0.0))


class Ichimoku(Strategy):
    """Облако Ишимоку: цена над облаком и тенкан выше кидзюна → лонг; цена под облаком или разворот линий → выход."""
    family = "ichimoku"
    description = "Ишимоку: покупка над облаком при тенкан выше кидзюна; выход под облаком или при обратном пересечении."

    @classmethod
    def default_params(cls):
        return {"tenkan": 9, "kijun": 26, "senkou": 52}

    @classmethod
    def param_grid(cls):
        return {"tenkan": [7, 9, 12], "kijun": [22, 26, 30], "senkou": [44, 52, 60]}

    def cadence_minutes(self):
        return 15

    def warmup(self):
        return self.params["senkou"] + self.params["kijun"] + 2

    @staticmethod
    def _mid(highs, lows, n):
        return (max(highs[-n:]) + min(lows[-n:])) / 2

    def decide(self, candles, context=None):
        highs, lows = _hl(candles)
        closes = _closes(candles)
        k = self.params["kijun"]
        if len(closes) < self.warmup():
            return hold("недостаточно данных")
        tenkan = self._mid(highs, lows, self.params["tenkan"])
        kijun = self._mid(highs, lows, k)
        # облако на текущей свече рассчитано kijun свечей назад
        h_past, l_past = highs[:-k], lows[:-k]
        span_a = (self._mid(h_past, l_past, self.params["tenkan"]) + self._mid(h_past, l_past, k)) / 2
        span_b = self._mid(h_past, l_past, self.params["senkou"])
        top, bottom = max(span_a, span_b), min(span_a, span_b)
        c = closes[-1]
        if c > top and tenkan > kijun:
            return Signal(Action.BUY, 1.0, min(1.0, (c - top) / top * 100), f"цена {c:.0f} над облаком {top:.0f}, тенкан {tenkan:.0f} > кидзюн {kijun:.0f}")
        if c < bottom or tenkan < kijun:
            return Signal(Action.SELL, 0.0, 0.7, f"{'цена под облаком ' + f'{bottom:.0f}' if c < bottom else 'тенкан ниже кидзюна'}, выходим")
        return hold(f"цена {c:.0f} у облака {bottom:.0f}–{top:.0f}, ждём", (context or {}).get("exposure", 0.0))


class EmaRibbon(Strategy):
    """Лента EMA: все линии выстроены по росту → лонг; лента рассыпалась → выход."""
    family = "ema_ribbon"
    description = "EMA-лента 8/13/21/34/55: покупка, когда лента выстроена по росту; выход, когда быстрые EMA ушли под медленные."

    @classmethod
    def default_params(cls):
        return {"base": 8, "steps": 5, "min_aligned": 4}

    @classmethod
    def param_grid(cls):
        return {"base": [5, 8, 10], "steps": [4, 5, 6], "min_aligned": [3, 4, 5]}

    def cadence_minutes(self):
        return 10

    def _periods(self):
        a, b, out = self.params["base"], int(self.params["base"] * 1.6), []
        for _ in range(self.params["steps"]):
            out.append(a)
            a, b = b, a + b
        return out

    def warmup(self):
        return self._periods()[-1] + 5

    def decide(self, candles, context=None):
        closes = _closes(candles)
        emas = [ind.ema(closes, p)[-1] for p in self._periods()]
        if any(e is None for e in emas):
            return hold("недостаточно данных")
        aligned = sum(1 for i in range(len(emas) - 1) if emas[i] > emas[i + 1])
        need = min(self.params["min_aligned"], len(emas) - 1)
        if aligned >= need and closes[-1] > emas[0]:
            return Signal(Action.BUY, 1.0, aligned / (len(emas) - 1), f"лента EMA выстроена по росту ({aligned} из {len(emas) - 1})")
        if emas[0] < emas[len(emas) // 2]:
            return Signal(Action.SELL, 0.0, 0.7, "быстрая EMA под серединой ленты, выходим")
        return hold(f"лента смешана ({aligned} из {len(emas) - 1}), ждём", (context or {}).get("exposure", 0.0))


def _dmi(highs, lows, closes, period):
    """ADX, +DI, −DI по Уайлдеру."""
    n = len(closes)
    if n < period * 2 + 2:
        return None, None, None
    tr, pdm, mdm = [], [], []
    for i in range(1, n):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))

    def wilder(vals):
        s = sum(vals[:period])
        out = [s]
        for v in vals[period:]:
            s = s - s / period + v
            out.append(s)
        return out
    trs, pds, mds = wilder(tr), wilder(pdm), wilder(mdm)
    pdi = [100 * p / t if t else 0.0 for p, t in zip(pds, trs)]
    mdi = [100 * m / t if t else 0.0 for m, t in zip(mds, trs)]
    dx = [100 * abs(p - m) / (p + m) if (p + m) else 0.0 for p, m in zip(pdi, mdi)]
    if len(dx) < period:
        return None, None, None
    adx = sum(dx[:period]) / period
    for v in dx[period:]:
        adx = (adx * (period - 1) + v) / period
    return adx, pdi[-1], mdi[-1]


class AdxDmi(Strategy):
    """ADX подтверждает силу тренда, +DI/−DI задают направление."""
    family = "adx_dmi"
    description = "ADX/DMI: покупка при сильном тренде (ADX выше порога) и +DI выше −DI; выход, когда −DI берёт верх или тренд затухает."

    @classmethod
    def default_params(cls):
        return {"period": 14, "adx_min": 25, "adx_exit": 18}

    @classmethod
    def param_grid(cls):
        return {"period": [10, 14, 20], "adx_min": [20, 25, 30], "adx_exit": [15, 18, 22]}

    def cadence_minutes(self):
        return 10

    def warmup(self):
        return self.params["period"] * 3 + 5

    def decide(self, candles, context=None):
        highs, lows = _hl(candles)
        adx, pdi, mdi = _dmi(highs, lows, _closes(candles), self.params["period"])
        if adx is None:
            return hold("недостаточно данных")
        if adx >= self.params["adx_min"] and pdi > mdi:
            return Signal(Action.BUY, 1.0, min(1.0, adx / 50), f"ADX={adx:.0f} сильный тренд, +DI {pdi:.0f} > −DI {mdi:.0f}")
        if mdi > pdi or adx < self.params["adx_exit"]:
            return Signal(Action.SELL, 0.0, 0.6, f"{'−DI выше +DI' if mdi > pdi else f'ADX={adx:.0f} тренд затух'}, выходим")
        return hold(f"ADX={adx:.0f}, +DI {pdi:.0f}, −DI {mdi:.0f}, ждём", (context or {}).get("exposure", 0.0))


class Squeeze(Strategy):
    """TTM Squeeze: полосы Боллинджера внутри канала Кельтнера = сжатие; выход из сжатия с моментумом вверх → лонг."""
    family = "squeeze"
    description = "Сжатие полос: Боллинджер внутри Кельтнера копит энергию; выход из сжатия с моментумом вверх — покупка, моментум вниз — выход."

    @classmethod
    def default_params(cls):
        return {"period": 20, "bb_mult": 2.0, "kc_mult": 1.5}

    @classmethod
    def param_grid(cls):
        return {"period": [14, 20, 30], "bb_mult": [1.8, 2.0, 2.2], "kc_mult": [1.2, 1.5, 2.0]}

    def cadence_minutes(self):
        return 10

    def warmup(self):
        return self.params["period"] * 2 + 5

    def decide(self, candles, context=None):
        closes = _closes(candles)
        highs, lows = _hl(candles)
        n = self.params["period"]
        lower, mid, upper = ind.bollinger(closes, n, self.params["bb_mult"])
        e = ind.ema(closes, n)
        a = ind.atr(highs, lows, closes, n)
        if upper[-1] is None or e[-1] is None or a[-1] is None or e[-2] is None or a[-2] is None:
            return hold("недостаточно данных")
        kc_up, kc_lo = e[-1] + self.params["kc_mult"] * a[-1], e[-1] - self.params["kc_mult"] * a[-1]
        kc_up_prev, kc_lo_prev = e[-2] + self.params["kc_mult"] * a[-2], e[-2] - self.params["kc_mult"] * a[-2]
        squeezed_now = upper[-1] < kc_up and lower[-1] > kc_lo
        squeezed_prev = upper[-2] is not None and upper[-2] < kc_up_prev and lower[-2] > kc_lo_prev
        # моментум: отклонение цены от середины между экстремумами и средней
        hh, ll = max(highs[-n:]), min(lows[-n:])
        mom = closes[-1] - ((hh + ll) / 2 + mid[-1]) / 2
        if squeezed_prev and not squeezed_now and mom > 0:
            return Signal(Action.BUY, 1.0, 0.8, f"выход из сжатия с моментумом вверх ({mom:+.0f})")
        if mom < 0 and not squeezed_now:
            return Signal(Action.SELL, 0.0, 0.6, f"моментум вниз ({mom:+.0f}), выходим")
        return hold("сжатие, копим энергию" if squeezed_now else f"моментум {mom:+.0f}, ждём", (context or {}).get("exposure", 0.0))


class VwapBreakout(Strategy):
    """Пробой скользящего VWAP с подтверждением объёмом."""
    family = "vwap_breakout"
    description = "Пробой VWAP: цена выше объёмно-взвешенной средней за сутки на заданный процент при объёме выше среднего — покупка; возврат под VWAP — выход."

    @classmethod
    def default_params(cls):
        return {"period": 24, "threshold_pct": 0.3, "vol_mult": 1.2}

    @classmethod
    def param_grid(cls):
        return {"period": [24, 48], "threshold_pct": [0.2, 0.3, 0.5], "vol_mult": [1.0, 1.2, 1.5]}

    def cadence_minutes(self):
        return 5

    def warmup(self):
        return self.params["period"] * 2 + 2

    def decide(self, candles, context=None):
        n = self.params["period"]
        win = candles[-n:]
        vol = sum(c.volume for c in win)
        if len(candles) < self.warmup() or vol <= 0:
            return hold("недостаточно данных")
        vwap = sum((c.high + c.low + c.close) / 3 * c.volume for c in win) / vol
        avg_vol = sum(c.volume for c in candles[-2 * n:-n]) / n if len(candles) >= 2 * n else vol / n
        c = candles[-1]
        dev = (c.close / vwap - 1) * 100
        if dev >= self.params["threshold_pct"] and c.volume >= self.params["vol_mult"] * avg_vol:
            return Signal(Action.BUY, 1.0, min(1.0, dev / 2), f"пробой VWAP {vwap:.0f} на {dev:+.2f}% при объёме x{c.volume / max(avg_vol, 1e-9):.1f}")
        if c.close < vwap:
            return Signal(Action.SELL, 0.0, 0.6, f"цена {c.close:.0f} вернулась под VWAP {vwap:.0f}")
        return hold(f"цена над VWAP на {dev:+.2f}%, ждём подтверждения", (context or {}).get("exposure", 0.0))


class StochRsi(Strategy):
    """Стохастический RSI: пересечение K над D в зоне перепроданности → лонг; K под D в перекупленности → выход."""
    family = "stoch_rsi"
    description = "Стохастический RSI: покупка при пересечении K над D в зоне перепроданности; выход при обратном пересечении в перекупленности."

    @classmethod
    def default_params(cls):
        return {"rsi": 14, "stoch": 14, "k": 3, "d": 3, "low": 20, "high": 80}

    @classmethod
    def param_grid(cls):
        return {"rsi": [10, 14, 21], "stoch": [10, 14], "low": [15, 20, 25], "high": [75, 80, 85]}

    def cadence_minutes(self):
        return 5

    def warmup(self):
        return self.params["rsi"] + self.params["stoch"] + self.params["k"] + self.params["d"] + 5

    def decide(self, candles, context=None):
        closes = _closes(candles)
        r = [x for x in ind.rsi(closes, self.params["rsi"]) if x is not None]
        n = self.params["stoch"]
        if len(r) < n + self.params["k"] + self.params["d"] + 2:
            return hold("недостаточно данных")
        stoch = []
        for i in range(n, len(r) + 1):
            w = r[i - n:i]
            lo, hi = min(w), max(w)
            stoch.append(100 * (w[-1] - lo) / (hi - lo) if hi > lo else 50.0)
        k = [sum(stoch[i - self.params["k"]:i]) / self.params["k"] for i in range(self.params["k"], len(stoch) + 1)]
        d = [sum(k[i - self.params["d"]:i]) / self.params["d"] for i in range(self.params["d"], len(k) + 1)]
        k_now, k_prev, d_now, d_prev = k[-1], k[-2], d[-1], d[-2]
        if k_prev <= d_prev and k_now > d_now and k_now < self.params["low"] + 15:
            return Signal(Action.BUY, 1.0, 0.7, f"стох-RSI K={k_now:.0f} пересёк D={d_now:.0f} снизу в зоне перепроданности")
        if k_prev >= d_prev and k_now < d_now and k_now > self.params["high"] - 15:
            return Signal(Action.SELL, 0.0, 0.7, f"стох-RSI K={k_now:.0f} пересёк D={d_now:.0f} сверху в зоне перекупленности")
        return hold(f"стох-RSI K={k_now:.0f} D={d_now:.0f}, ждём", (context or {}).get("exposure", 0.0))


class ChaikinTrend(Strategy):
    """Денежный поток Чайкина подтверждает тренд по EMA."""
    family = "cmf_trend"
    description = "Денежный поток Чайкина + EMA: покупка, когда деньги входят в рынок (CMF выше порога) и цена над EMA; выход при оттоке или уходе под EMA."

    @classmethod
    def default_params(cls):
        return {"cmf": 20, "ema": 50, "enter": 0.10, "exit": -0.05}

    @classmethod
    def param_grid(cls):
        return {"cmf": [14, 20, 30], "ema": [34, 50, 100], "enter": [0.05, 0.10, 0.15]}

    def cadence_minutes(self):
        return 10

    def warmup(self):
        return max(self.params["cmf"], self.params["ema"]) + 5

    def decide(self, candles, context=None):
        n = self.params["cmf"]
        win = candles[-n:]
        vol = sum(c.volume for c in win)
        closes = _closes(candles)
        e = ind.ema(closes, self.params["ema"])[-1]
        if len(candles) < self.warmup() or vol <= 0 or e is None:
            return hold("недостаточно данных")
        mfv = sum(((c.close - c.low) - (c.high - c.close)) / (c.high - c.low) * c.volume if c.high > c.low else 0.0 for c in win)
        cmf = mfv / vol
        c = closes[-1]
        if cmf >= self.params["enter"] and c > e:
            return Signal(Action.BUY, 1.0, min(1.0, cmf * 4), f"CMF={cmf:+.2f} деньги входят, цена над EMA{self.params['ema']}")
        if cmf <= self.params["exit"] or c < e:
            return Signal(Action.SELL, 0.0, 0.6, f"{'CMF=' + f'{cmf:+.2f}' + ' отток' if cmf <= self.params['exit'] else 'цена под EMA'}, выходим")
        return hold(f"CMF={cmf:+.2f}, ждём подтверждения", (context or {}).get("exposure", 0.0))


COMMUNITY_STRATEGIES: list[type[Strategy]] = [BollingerRsi, Ichimoku, EmaRibbon, AdxDmi, Squeeze, VwapBreakout, StochRsi, ChaikinTrend]
