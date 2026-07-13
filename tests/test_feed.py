from __future__ import annotations

import json

import pytest

from hobo.adapters.okx import constants as c
from hobo.adapters.okx.parsing import parse_public
from hobo.adapters.okx.websocket import (
    OkxPublicClient,
    funding_rate_subscription,
    mark_price_subscription,
)
from hobo.risk.staleness import StalenessWatchdog


# --- inbound public-channel parsing ---


def test_parse_mark_price_message():
    raw = json.dumps(
        {
            "arg": {"channel": "mark-price", "instId": "BTC-USDT-SWAP"},
            "data": [{"instType": "SWAP", "instId": "BTC-USDT-SWAP", "markPx": "50123.4", "ts": "1597026383085"}],
        }
    )
    parsed = parse_public(raw)
    assert parsed.key == c.MARK_PRICE_CHANNEL
    assert parsed.data["instrument_id"] == "BTC-USDT-SWAP"
    assert parsed.data["mark_price"] == pytest.approx(50123.4)
    assert parsed.data["ts_ns"] == 1597026383085 * 1_000_000
    assert not parsed.is_control and not parsed.is_error and not parsed.is_heartbeat


def test_parse_funding_rate_message():
    raw = json.dumps(
        {
            "arg": {"channel": "funding-rate", "instId": "BTC-USDT-SWAP"},
            "data": [
                {
                    "instType": "SWAP",
                    "instId": "BTC-USDT-SWAP",
                    "fundingRate": "0.0001",
                    "fundingTime": "1622009600000",
                    "ts": "1622006400000",
                }
            ],
        }
    )
    parsed = parse_public(raw)
    assert parsed.key == c.FUNDING_RATE_CHANNEL
    assert parsed.data["instrument_id"] == "BTC-USDT-SWAP"
    assert parsed.data["funding_rate"] == pytest.approx(0.0001)
    assert parsed.data["funding_time_ns"] == 1622009600000 * 1_000_000


def test_parse_pong():
    parsed = parse_public(c.PONG_MESSAGE)
    assert parsed.key == c.HEARTBEAT_KEY
    assert parsed.is_heartbeat is True


def test_parse_subscribe_ack():
    raw = json.dumps({"event": "subscribe", "arg": {"channel": "mark-price", "instId": "BTC-USDT-SWAP"}})
    parsed = parse_public(raw)
    assert parsed.key == c.SUBSCRIBE_ACK_KEY
    assert parsed.is_control is True
    assert parsed.is_error is False


def test_parse_error_event():
    raw = json.dumps({"event": "error", "code": "60012", "msg": "bad request"})
    parsed = parse_public(raw)
    assert parsed.key == c.ERROR_KEY
    assert parsed.is_control is True
    assert parsed.is_error is True


def test_parse_malformed_json_is_ignored_not_raised():
    parsed = parse_public("not json{{{")
    assert parsed.key == c.IGNORED_KEY
    assert parsed.is_control is True
    assert parsed.is_error is False


def test_parse_empty_data_is_ignored():
    raw = json.dumps({"arg": {"channel": "mark-price", "instId": "BTC-USDT-SWAP"}, "data": []})
    parsed = parse_public(raw)
    assert parsed.key == c.IGNORED_KEY


# --- OkxPublicClient: transport-only wiring ---


def test_subscription_builders_shape():
    assert mark_price_subscription("BTC-USDT-SWAP") == {"channel": "mark-price", "instId": "BTC-USDT-SWAP"}
    assert funding_rate_subscription("BTC-USDT-SWAP") == {"channel": "funding-rate", "instId": "BTC-USDT-SWAP"}


def test_public_client_build_and_parse_delegate_to_okx_codec():
    client = OkxPublicClient("wss://example.invalid")
    assert client.build_ping_message() == c.PING_MESSAGE

    subscribe_msg = json.loads(client.build_subscribe_message([mark_price_subscription("BTC-USDT-SWAP")]))
    assert subscribe_msg["op"] == c.OP_SUBSCRIBE
    assert subscribe_msg["args"] == [{"channel": "mark-price", "instId": "BTC-USDT-SWAP"}]

    assert client.parse_message(c.PONG_MESSAGE).is_heartbeat is True


# --- staleness watchdog ---


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def test_staleness_watchdog_trips_after_threshold():
    clock = FakeClock()
    watchdog = StalenessWatchdog(threshold_s=5.0, clock=clock)
    watchdog.record_message()

    clock.advance(3.0)
    assert watchdog.check() is False
    assert watchdog.stale is False

    clock.advance(3.0)  # total 6s since last message
    assert watchdog.check() is True
    assert watchdog.stale is True


def test_staleness_watchdog_clears_on_new_message():
    clock = FakeClock()
    watchdog = StalenessWatchdog(threshold_s=5.0, clock=clock)
    watchdog.record_message()
    clock.advance(10.0)
    watchdog.check()
    assert watchdog.stale is True

    cleared = watchdog.record_message()
    assert cleared is True
    assert watchdog.stale is False


def test_staleness_watchdog_no_messages_yet_never_trips():
    clock = FakeClock()
    watchdog = StalenessWatchdog(threshold_s=5.0, clock=clock)
    clock.advance(100.0)
    assert watchdog.check() is False
    assert watchdog.stale is False


def test_staleness_watchdog_age_reports_none_before_first_message():
    assert StalenessWatchdog().age() is None
