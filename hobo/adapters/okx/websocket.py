"""OKX WebSocket transport: the public and private WS clients (subclasses of the
generic AbstractWebSocketClient), plus the small outbound message builders and the
private-channel login signing they send. Inbound parsing lives in parsing.py.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid

from hobo.adapters.okx import constants as c
from hobo.adapters.okx import parsing
from hobo.adapters.transport.ws_base import AbstractWebSocketClient, ParsedMessage


# --- outbound message builders + login signing ---


def build_subscribe_message(subscriptions: list[dict]) -> str:
    return json.dumps({"op": c.OP_SUBSCRIBE, "args": subscriptions})


def _login_sign(secret: str, timestamp: str) -> str:
    message = f"{timestamp}{c.LOGIN_SIGN_METHOD}{c.LOGIN_SIGN_PATH}".encode()
    return base64.b64encode(hmac.new(secret.encode(), message, hashlib.sha256).digest()).decode()


def build_login_message(api_key: str, secret: str, passphrase: str, timestamp: str | None = None) -> str:
    ts = timestamp if timestamp is not None else str(int(time.time()))
    args = {"apiKey": api_key, "passphrase": passphrase, "timestamp": ts, "sign": _login_sign(secret, ts)}
    return json.dumps({"op": c.OP_LOGIN, "args": [args]})


def mark_price_subscription(instrument_id: str) -> dict:
    return {"channel": c.MARK_PRICE_CHANNEL, "instId": instrument_id}


def funding_rate_subscription(instrument_id: str) -> dict:
    return {"channel": c.FUNDING_RATE_CHANNEL, "instId": instrument_id}


# --- public WS: transport for the public channels (caller declares subs + handlers) ---


class OkxPublicClient(AbstractWebSocketClient):
    def build_subscribe_message(self, subscriptions: list[dict]) -> str:
        return build_subscribe_message(subscriptions)

    def build_ping_message(self) -> str:
        return c.PING_MESSAGE

    def parse_message(self, raw: str) -> ParsedMessage:
        return parsing.parse_public(raw)


# --- private WS: authenticated channel for order placement + order/fill push ---


class OkxPrivateClient(AbstractWebSocketClient):
    """Handshake: connect -> login (_after_connect) -> on login ack, subscribe the
    configured channels -> receive order updates. Auto-resubscribe is off so nothing
    is sent before login succeeds; the same handshake re-runs on every reconnect."""

    def __init__(self, url: str, api_key: str, api_secret: str, passphrase: str, **kwargs) -> None:
        super().__init__(url, **kwargs)
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self.private_subscriptions: list[dict] = []
        self.logged_in = asyncio.Event()
        self.on(c.LOGIN_KEY, self._on_login_ack)

    def subscribe_private(self, channel: dict) -> None:
        """Private channels are login-gated, so they are sent after the login ack
        rather than through the base's connect-time auto-subscribe."""
        self.private_subscriptions.append(channel)

    async def _after_connect(self, ws) -> None:
        self.logged_in.clear()
        await ws.send(build_login_message(self._api_key, self._api_secret, self._passphrase))

    def _on_login_ack(self, _msg: ParsedMessage) -> None:
        self.logged_in.set()
        if self.private_subscriptions:
            asyncio.ensure_future(self.send(build_subscribe_message(self.private_subscriptions)))

    async def place_order(self, args: dict) -> None:
        await self.send(json.dumps({"id": uuid.uuid4().hex[:16], "op": c.OP_ORDER, "args": [args]}))

    async def cancel_order(self, args: dict) -> None:
        await self.send(json.dumps({"id": uuid.uuid4().hex[:16], "op": c.OP_CANCEL_ORDER, "args": [args]}))

    def build_subscribe_message(self, subscriptions: list[dict]) -> str:
        return build_subscribe_message(subscriptions)

    def build_ping_message(self) -> str:
        return c.PING_MESSAGE

    def parse_message(self, raw: str) -> ParsedMessage:
        return parsing.parse_private(raw)
