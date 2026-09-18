"""Внешние данные для директора и аналитиков: индекс страха и жадности, открытый интерес, объём."""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


def fetch_macro(symbol: str = "BTCUSDT", http=None) -> dict:
    import httpx
    out = {"ts": int(time.time())}
    c = http or httpx.Client(timeout=10.0)
    try:
        try:
            r = c.get("https://api.alternative.me/fng/?limit=2")
            d = r.json()["data"]
            out["fng"] = int(d[0]["value"])
            out["fng_label"] = {"Extreme Fear": "крайний страх", "Fear": "страх", "Neutral": "нейтрально", "Greed": "жадность", "Extreme Greed": "крайняя жадность"}.get(d[0]["value_classification"], d[0]["value_classification"])
            out["fng_prev"] = int(d[1]["value"]) if len(d) > 1 else None
        except Exception as e:  # noqa: BLE001
            log.warning("индекс страха и жадности недоступен: %s", e)
        try:
            r = c.get("https://fapi.binance.com/fapi/v1/openInterest", params={"symbol": symbol})
            out["open_interest"] = float(r.json()["openInterest"])
            r = c.get("https://fapi.binance.com/futures/data/openInterestHist", params={"symbol": symbol, "period": "1d", "limit": 2})
            hist = r.json()
            if len(hist) >= 2:
                out["oi_change_pct"] = round((float(hist[-1]["sumOpenInterest"]) / float(hist[-2]["sumOpenInterest"]) - 1) * 100, 2)
        except Exception as e:  # noqa: BLE001
            log.warning("открытый интерес недоступен: %s", e)
        try:
            r = c.get("https://api.binance.com/api/v3/ticker/24hr", params={"symbol": symbol})
            j = r.json()
            out["volume_24h_usd"] = float(j["quoteVolume"])
            out["change_24h_pct"] = float(j["priceChangePercent"])
        except Exception as e:  # noqa: BLE001
            log.warning("суточный объём недоступен: %s", e)
    finally:
        if http is None:
            c.close()
    return out


def describe(m: dict | None) -> str:
    if not m:
        return "внешние данные недоступны"
    parts = []
    if m.get("fng") is not None:
        parts.append(f"индекс страха и жадности {m['fng']} ({m.get('fng_label', '')})")
    if m.get("open_interest") is not None:
        parts.append(f"открытый интерес {m['open_interest']:.0f} BTC" + (f" ({m['oi_change_pct']:+.1f}% за день)" if m.get("oi_change_pct") is not None else ""))
    if m.get("volume_24h_usd") is not None:
        parts.append(f"объём за сутки {m['volume_24h_usd']/1e9:.2f} млрд $")
    return "; ".join(parts) or "внешние данные недоступны"
