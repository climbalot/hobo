from __future__ import annotations

import abc
import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

import websockets

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedMessage:
    key: str
    data: dict | None = None
    is_heartbeat: bool = False
    is_control: bool = False
    is_error: bool = False


Handler = Callable[[ParsedMessage], None]


class AbstractWebSocketClient(abc.ABC):
    def __init__(
        self,
        url: str,
        *,
        should_reconnect: bool = True,
        should_auto_resubscribe: bool = True,
        reconnect_base_secs: float = 0.5,
        reconnect_max_secs: float = 30.0,
        reconnect_reset_after_secs: float = 60.0,
        max_reconnect_attempts: int | None = None,
        app_ping_interval_secs: float | None = 20.0,
        protocol_ping_interval_secs: float | None = None,
        protocol_ping_timeout_secs: float | None = None,
        read_timeout_secs: float | None = 30.0,
    ) -> None:
        self.url = url
        self.should_reconnect = should_reconnect
        self.should_auto_resubscribe = should_auto_resubscribe
        self.reconnect_base_secs = reconnect_base_secs
        self.reconnect_max_secs = reconnect_max_secs
        self.reconnect_reset_after_secs = reconnect_reset_after_secs
        self.max_reconnect_attempts = max_reconnect_attempts
        self.app_ping_interval_secs = app_ping_interval_secs
        self.protocol_ping_interval_secs = protocol_ping_interval_secs
        self.protocol_ping_timeout_secs = protocol_ping_timeout_secs
        self.read_timeout_secs = read_timeout_secs

        self.subscriptions: list[dict] = []
        self._handlers: dict[str, Handler] = {}
        self.on_connect: Callable[[], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None

        self.stop_event = asyncio.Event()
        self._ws: websockets.ClientConnection | None = None

    @abc.abstractmethod
    def build_subscribe_message(self, subscriptions: list[dict]) -> str: ...

    @abc.abstractmethod
    def build_ping_message(self) -> str: ...

    @abc.abstractmethod
    def parse_message(self, raw: str) -> ParsedMessage: ...

    async def _after_connect(self, ws) -> None:
        """Post-connect handshake hook, run before auto-subscribe. Override for
        auth (e.g. send a login message). Only sends here - responses arrive
        through the normal receive/dispatch path once _run_io starts."""

    def on(self, key: str, handler: Handler) -> None:
        self._handlers[key] = handler

    def subscribe(self, subscription: dict) -> None:
        self.subscriptions.append(subscription)

    def stop(self) -> None:
        self.stop_event.set()
        if self._ws is not None:
            asyncio.ensure_future(self._ws.close())

    async def send(self, message: str) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("websocket not connected")
        await ws.send(message)

    async def run(self) -> None:
        attempt = 0
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self.stop_event.is_set():
                    logger.warning("ws session ended", exc_info=True, extra={"url": self.url})
            if self.stop_event.is_set() or not self.should_reconnect:
                return
            if self.max_reconnect_attempts is not None and attempt >= self.max_reconnect_attempts:
                logger.error("ws giving up after max reconnect attempts", extra={"attempts": attempt})
                return
            if time.monotonic() - started >= self.reconnect_reset_after_secs:
                attempt = 0
            await asyncio.sleep(self._reconnect_delay(attempt))
            attempt += 1

    def _reconnect_delay(self, attempt: int) -> float:
        delay = min(self.reconnect_base_secs * 2**attempt, self.reconnect_max_secs)
        return max(0.0, delay * (1 + random.uniform(-0.2, 0.2)))

    async def _session(self) -> None:
        async with websockets.connect(
            self.url,
            ping_interval=self.protocol_ping_interval_secs,
            ping_timeout=self.protocol_ping_timeout_secs,
        ) as ws:
            self._ws = ws
            try:
                await self._after_connect(ws)
                if self.subscriptions and self.should_auto_resubscribe:
                    await ws.send(self.build_subscribe_message(self.subscriptions))
                if self.on_connect is not None:
                    self.on_connect()
                await self._run_io(ws)
            finally:
                self._ws = None
                if self.on_disconnect is not None:
                    self.on_disconnect()

    async def _run_io(self, ws) -> None:
        tasks = [asyncio.create_task(self._receive_loop(ws))]
        if self.app_ping_interval_secs is not None:
            tasks.append(asyncio.create_task(self._ping_loop(ws)))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _receive_loop(self, ws) -> None:
        while not self.stop_event.is_set():
            try:
                if self.read_timeout_secs is None:
                    raw = await ws.recv()
                else:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.read_timeout_secs)
            except asyncio.TimeoutError:
                logger.warning("ws read timeout, reconnecting", extra={"timeout_secs": self.read_timeout_secs})
                await ws.close()
                return
            self._dispatch(raw)

    async def _ping_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(self.app_ping_interval_secs)
            await ws.send(self.build_ping_message())

    def _dispatch(self, raw: str) -> None:
        msg = self.parse_message(raw)
        if msg.is_error:
            logger.warning("ws error message", extra={"data": msg.data})
            return
        if msg.is_heartbeat or msg.is_control:
            return
        handler = self._handlers.get(msg.key)
        if handler is not None:
            handler(msg)
