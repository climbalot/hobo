"""OKX market-data capability: `fetch_mark` (a synchronous REST snapshot, usable
before the WS is up) and `stream` (subscribes the public WS, translating
marks/funding into MarkEvent/FundingEvent onto the bus). Pure translation - no
state, no risk; the WS client is handed to the adapter's run loop via `register_ws`.
"""

from __future__ import annotations

from collections.abc import Callable

from hobo.adapters.base import MarketData
from hobo.adapters.okx import constants as c
from hobo.adapters.okx.rest import OkxRestClient
from hobo.adapters.okx.websocket import (
    OkxPublicClient,
    funding_rate_subscription,
    mark_price_subscription,
)
from hobo.core.bus import EventBus
from hobo.core.events import FundingEvent, MarkEvent


class MarketDataError(RuntimeError):
    pass


def parse_mark_price(response: dict, instrument_id: str) -> tuple[float, int]:
    match = next((e for e in response.get("data") or [] if e.get("instId") == instrument_id), None)
    if match is None:
        raise MarketDataError(f"no mark price returned for {instrument_id!r}")
    try:
        return float(match["markPx"]), int(match["ts"]) * 1_000_000
    except (KeyError, ValueError) as exc:
        raise MarketDataError(f"malformed mark price for {instrument_id!r}: {exc}") from exc


class OkxMarketData(MarketData):
    def __init__(self, rest: OkxRestClient, ws_url: str, register_ws: Callable[[OkxPublicClient], None]) -> None:
        self._rest = rest
        self._ws_url = ws_url
        self._register_ws = register_ws
        self._bus: EventBus | None = None

    def fetch_mark(self, instrument_id: str) -> tuple[float, int]:
        return parse_mark_price(self._rest.get_mark_price(c.INST_TYPE_SWAP, instrument_id), instrument_id)

    def stream(self, bus: EventBus, instrument_ids: list[str]) -> None:
        self._bus = bus
        public = OkxPublicClient(self._ws_url)
        for instrument_id in instrument_ids:  # one WS connection, many instruments
            public.subscribe(mark_price_subscription(instrument_id))
            public.subscribe(funding_rate_subscription(instrument_id))
        public.on(c.MARK_PRICE_CHANNEL, self._on_mark)
        public.on(c.FUNDING_RATE_CHANNEL, self._on_funding)
        self._register_ws(public)  # adapter's run loop drives the connection

    def _on_mark(self, parsed) -> None:
        d = parsed.data
        self._bus.publish(MarkEvent(d["instrument_id"], d["mark_price"], d["ts_ns"]))

    def _on_funding(self, parsed) -> None:
        d = parsed.data
        self._bus.publish(FundingEvent(d["instrument_id"], d["funding_rate"], d["funding_time_ns"], d["ts_ns"]))
