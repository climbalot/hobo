from __future__ import annotations

import asyncio
import json

import websockets

from hobo.adapters.transport.ws_base import AbstractWebSocketClient, ParsedMessage


class FakeWsClient(AbstractWebSocketClient):
    """Concrete subclass so the base's pure/sync dispatch logic can be unit-tested
    without a real connection. Tests monkeypatch `parse_message` per case."""

    def build_subscribe_message(self, subscriptions: list[dict]) -> str:
        return json.dumps({"op": "subscribe", "args": subscriptions})

    def build_ping_message(self) -> str:
        return "ping"

    def parse_message(self, raw: str) -> ParsedMessage:  # pragma: no cover - overridden per test
        raise NotImplementedError


def make_client(**kwargs) -> FakeWsClient:
    return FakeWsClient("wss://example.invalid", **kwargs)


# --- dispatch routing ---


def test_dispatch_routes_data_message_to_registered_handler():
    client = make_client()
    received = []
    client.on("ticker", received.append)
    client.parse_message = lambda raw: ParsedMessage(key="ticker", data={"px": 100})

    client._dispatch("irrelevant-raw")
    assert len(received) == 1
    assert received[0].data == {"px": 100}


def test_dispatch_data_message_with_no_handler_does_not_raise():
    client = make_client()
    client.parse_message = lambda raw: ParsedMessage(key="unregistered")
    client._dispatch("irrelevant-raw")  # should not raise


def test_dispatch_does_not_route_error_to_key_handlers():
    client = make_client()
    seen = []
    client.on("error", seen.append)
    client.parse_message = lambda raw: ParsedMessage(key="error", is_control=True, is_error=True)
    client._dispatch("irrelevant-raw")
    assert seen == []  # error messages short-circuit before key handler dispatch


def test_dispatch_does_not_route_heartbeat_or_control_to_key_handlers():
    client = make_client()
    seen = []
    client.on("pong", seen.append)
    client.on("subscribe_ack", seen.append)
    client.parse_message = lambda raw: ParsedMessage(key="pong", is_heartbeat=True)
    client._dispatch("hb")
    client.parse_message = lambda raw: ParsedMessage(key="subscribe_ack", is_control=True)
    client._dispatch("ack")
    assert seen == []


def test_subscribe_appends_subscription():
    client = make_client()
    client.subscribe({"channel": "ticker"})
    client.subscribe({"channel": "trades"})
    assert client.subscriptions == [{"channel": "ticker"}, {"channel": "trades"}]


# --- reconnect backoff (grows and caps) ---


def test_reconnect_delay_grows_and_caps():
    client = make_client(reconnect_base_secs=0.5, reconnect_max_secs=30.0)
    first = client._reconnect_delay(0)
    assert 0 <= first <= 0.5 * 1.2
    # large attempt saturates at the cap (plus jitter band)
    assert client._reconnect_delay(20) <= 30.0 * 1.2
    assert client._reconnect_delay(20) >= 30.0 * 0.8


# --- real end-to-end session lifecycle against a local WS server ---


class EchoClient(AbstractWebSocketClient):
    def build_subscribe_message(self, subscriptions: list[dict]) -> str:
        return json.dumps({"op": "subscribe", "args": subscriptions})

    def build_ping_message(self) -> str:
        return "ping"

    def parse_message(self, raw: str) -> ParsedMessage:
        return ParsedMessage(key="echo", data={"raw": raw})


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.02) -> None:
    async def _poll():
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def test_full_session_lifecycle_against_real_server():
    received, connected, disconnected = [], [], []

    async def handler(websocket):
        await websocket.send("hello")
        async for _ in websocket:
            pass  # drain subscribe/ping traffic

    async with websockets.serve(handler, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = EchoClient(
            f"ws://localhost:{port}",
            should_reconnect=False,
            app_ping_interval_secs=None,
        )
        client.on("echo", lambda pm: received.append(pm.data["raw"]))
        client.on_connect = lambda: connected.append(True)
        client.on_disconnect = lambda: disconnected.append(True)

        run_task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: len(received) >= 1)
            assert received == ["hello"]
            assert connected == [True]

            client.stop()
            await asyncio.wait_for(run_task, timeout=2)
        finally:
            if not run_task.done():
                run_task.cancel()

    assert disconnected == [True]
