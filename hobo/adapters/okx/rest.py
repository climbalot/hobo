"""OKX REST client: v5 endpoints + HMAC signing over AbstractRestClient. Returns
raw response dicts (no domain mapping); public calls are unsigned, account calls
pass signed=True.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone

import httpx

from hobo.adapters.transport.rest_base import AbstractRestClient

INSTRUMENTS_PATH = "/api/v5/public/instruments"
MARK_PRICE_PATH = "/api/v5/public/mark-price"
POSITIONS_PATH = "/api/v5/account/positions"
BALANCE_PATH = "/api/v5/account/balance"
FILLS_PATH = "/api/v5/trade/fills-history"  # longer retention than /fills, for full rebuild


def _iso_timestamp() -> str:
    # OKX wants ISO-8601 UTC with millisecond precision and a 'Z' suffix.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clean(params: dict) -> dict:
    return {k: v for k, v in params.items() if v is not None}


class OkxRestClient(AbstractRestClient):
    def __init__(
        self,
        rest_url: str,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        demo_trading: bool = False,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(rest_url, transport=transport)
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._demo_trading = demo_trading

    # --- auth surface (OKX REST: distinct from the WS login sign) ---

    def default_headers(self) -> dict[str, str]:
        # Demo selector on EVERY request, incl. unsigned public ones - else public endpoints return production data.
        return {"x-simulated-trading": "1"} if self._demo_trading else {}

    def get_signature(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        prehash = f"{timestamp}{method}{request_path}{body}".encode()
        digest = hmac.new(self._api_secret.encode(), prehash, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def get_auth_headers(self, method: str, request_path: str, body: str) -> dict[str, str]:
        timestamp = _iso_timestamp()
        return {
            "OK-ACCESS-KEY": self._api_key,
            "OK-ACCESS-SIGN": self.get_signature(timestamp, method, request_path, body),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type": "application/json",
        }

    # --- public market data ---

    def get_instruments(self, inst_type: str, inst_id: str | None = None) -> dict:
        return self.get(INSTRUMENTS_PATH, params=_clean({"instType": inst_type, "instId": inst_id}))

    def get_mark_price(self, inst_type: str, inst_id: str | None = None) -> dict:
        return self.get(MARK_PRICE_PATH, params=_clean({"instType": inst_type, "instId": inst_id}))

    # --- account (signed) ---

    def get_positions(self, inst_type: str | None = None, inst_id: str | None = None) -> dict:
        return self.get(POSITIONS_PATH, params=_clean({"instType": inst_type, "instId": inst_id}), signed=True)

    def get_balance(self, ccy: str | None = None) -> dict:
        return self.get(BALANCE_PATH, params=_clean({"ccy": ccy}), signed=True)

    def get_fills(self, inst_type: str, limit: int = 100, after: str | None = None) -> dict:
        # `after` = billId; returns fills older than it (pagination toward history).
        return self.get(FILLS_PATH, params=_clean({"instType": inst_type, "limit": str(limit), "after": after}), signed=True)
