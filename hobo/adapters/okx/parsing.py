"""OKX inbound WS codec: parse a raw frame into a ParsedMessage, keeping all
knowledge of OKX's message shapes out of the transport (websocket.py).
`_parse_envelope` handles frames common to every connection; parse_public /
parse_private add each connection's channel/op routing.
"""

from __future__ import annotations

import json

from hobo.adapters.okx import constants as c
from hobo.adapters.transport.ws_base import ParsedMessage


def _parse_envelope(raw: str) -> tuple[ParsedMessage | None, dict | None]:
    """(terminal_message, None) for frames common to all OKX connections, or
    (None, decoded_dict) for channel/op frames the caller routes itself."""
    if raw == c.PONG_MESSAGE:
        return ParsedMessage(key=c.HEARTBEAT_KEY, is_heartbeat=True), None
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return ParsedMessage(key=c.IGNORED_KEY, data={"unparsed": raw}, is_control=True), None

    event = msg.get("event")
    if event == c.EVENT_ERROR:
        return ParsedMessage(key=c.ERROR_KEY, data=msg, is_control=True, is_error=True), None
    if event == c.OP_SUBSCRIBE:
        return ParsedMessage(key=c.SUBSCRIBE_ACK_KEY, data=msg, is_control=True), None
    return None, msg


def _decode_mark_price(entry: dict) -> dict:
    return {"instrument_id": entry["instId"], "mark_price": float(entry["markPx"]), "ts_ns": int(entry["ts"]) * 1_000_000}


def _decode_funding_rate(entry: dict) -> dict:
    return {
        "instrument_id": entry["instId"],
        "funding_rate": float(entry["fundingRate"]),
        "funding_time_ns": int(entry["fundingTime"]) * 1_000_000,
        "ts_ns": int(entry["ts"]) * 1_000_000,
    }


_PUBLIC_DECODERS = {c.MARK_PRICE_CHANNEL: _decode_mark_price, c.FUNDING_RATE_CHANNEL: _decode_funding_rate}


def parse_public(raw: str) -> ParsedMessage:
    terminal, msg = _parse_envelope(raw)
    if terminal is not None:
        return terminal
    channel = (msg.get("arg") or {}).get("channel")
    data = msg.get("data") or []
    decoder = _PUBLIC_DECODERS.get(channel)
    if not data or decoder is None:
        return ParsedMessage(key=c.IGNORED_KEY, data=msg, is_control=True)
    return ParsedMessage(key=channel, data=decoder(data[0]))


def parse_private(raw: str) -> ParsedMessage:
    terminal, msg = _parse_envelope(raw)
    if terminal is not None:
        return terminal

    if msg.get("event") == c.OP_LOGIN:
        if msg.get("code") == c.OK_CODE:
            return ParsedMessage(key=c.LOGIN_KEY, data=msg)
        return ParsedMessage(key=c.ERROR_KEY, data=msg, is_control=True, is_error=True)

    if msg.get("op") in (c.OP_ORDER, c.OP_CANCEL_ORDER):
        is_error = msg.get("code") not in (c.OK_CODE, None)
        return ParsedMessage(key=c.ORDER_ACK_KEY, data=msg, is_error=is_error)

    channel = (msg.get("arg") or {}).get("channel")
    data = msg.get("data") or []
    if channel == c.ORDERS_CHANNEL and data:
        return ParsedMessage(key=c.ORDERS_CHANNEL, data=data[0])
    return ParsedMessage(key=c.IGNORED_KEY, data=msg, is_control=True)
