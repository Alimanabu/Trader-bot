"""Мост к реальной бирже: Binance Spot (сначала тестовая сеть). Только рыночные ордера, только спот.

Сделки трейдеров со званием «Реальный счёт» зеркалятся на бирже пропорционально live_capital_usd.
Демосчёт остаётся источником истины для компании, реальный счёт повторяет его движения.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

log = logging.getLogger(__name__)

TESTNET_URL = "https://testnet.binance.vision"
MAINNET_URL = "https://api.binance.com"


class BrokerError(RuntimeError):
    pass


class BinanceSpotBroker:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True, http=None):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.base = TESTNET_URL if testnet else MAINNET_URL
        self.testnet = testnet
        self._http = http

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _client(self):
        if self._http is not None:
            return self._http
        import httpx
        return httpx.Client(timeout=15.0, base_url=self.base)

    def _signed(self, method: str, path: str, params: dict) -> dict:
        params = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        query = urlencode(params)
        sig = hmac.new(self.api_secret, query.encode(), hashlib.sha256).hexdigest()
        c = self._client()
        try:
            r = c.request(method, f"{path}?{query}&signature={sig}", headers={"X-MBX-APIKEY": self.api_key})
            if r.status_code >= 400:
                raise BrokerError(f"{r.status_code}: {r.text[:200]}")
            return r.json()
        finally:
            if self._http is None:
                c.close()

    def balances(self) -> dict[str, float]:
        data = self._signed("GET", "/api/v3/account", {})
        return {b["asset"]: float(b["free"]) for b in data.get("balances", []) if float(b["free"]) > 0}

    def market_buy(self, symbol: str, quote_usd: float) -> dict:
        return self._order(symbol, "BUY", {"quoteOrderQty": f"{quote_usd:.2f}"})

    def market_sell(self, symbol: str, qty: float) -> dict:
        return self._order(symbol, "SELL", {"quantity": f"{qty:.5f}"})

    def _order(self, symbol: str, side: str, extra: dict) -> dict:
        data = self._signed("POST", "/api/v3/order", {"symbol": symbol, "side": side, "type": "MARKET", **extra})
        filled = float(data.get("executedQty") or 0)
        quote = float(data.get("cummulativeQuoteQty") or 0)
        return {"order_id": data.get("orderId"), "side": side, "qty": filled, "quote": quote,
                "price": (quote / filled) if filled else 0.0, "status": data.get("status")}
