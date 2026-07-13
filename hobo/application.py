"""Application runtime: owns the process lifecycle - starts background tasks and
services, handles signals, tears down in order. `Application` runs the primary
engine; `ReplicaApplication` runs the warm standby that tails the log.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass

from hobo.adapters.base import ExchangeAdapter
from hobo.config import DurabilityConfig
from hobo.core.exchange_reconciler import ExchangeReconciler
from hobo.core.fill_reconciler import FillReconciler
from hobo.core.health import FeedHealthMonitor
from hobo.core.order_bridge import OrderBridge
from hobo.core.state_store import StateStore
from hobo.log import snapshot as snap
from hobo.log.writer import LogWriter
from hobo.obs.server import ColdPathServer
from hobo.replica.tail import ReplicaTail, run_replica_tail_loop
from hobo.risk.state import State

logger = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_SECS = 30.0
STALENESS_POLL_INTERVAL_SECS = 1.0
LOG_WAIT_POLL_SECS = 0.5


@dataclass
class EngineServices:
    """The built engine components the runtime drives. `reconciler`/`fill_reconciler`
    are set only when trading a real account."""

    store: StateStore
    writer: LogWriter
    monitor: FeedHealthMonitor
    cold_path_server: ColdPathServer
    order_bridge: OrderBridge | None = None
    reconciler: ExchangeReconciler | None = None
    fill_reconciler: FillReconciler | None = None


def _install_shutdown(on_signal: Callable[[], None]) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, on_signal)


async def _run_periodically(interval_secs: float, stop: asyncio.Event, action: Callable[[], None]) -> None:
    """Call `action` every `interval_secs` until `stop` is set."""
    while not stop.is_set():
        await asyncio.sleep(interval_secs)
        action()


class Application:
    def __init__(self, durability: DurabilityConfig, adapter: ExchangeAdapter, services: EngineServices) -> None:
        self._durability = durability
        self._adapter = adapter
        self._services = services
        self._last_snapshot_seq = services.store.state.last_seq
        self._stop = asyncio.Event()

    async def run(self) -> None:
        if self._services.order_bridge is not None:
            self._services.order_bridge.bind_loop(asyncio.get_running_loop())  # enable dashboard order entry
        startables = [self._services.cold_path_server, *([self._services.reconciler] if self._services.reconciler else [])]
        for service in startables:
            service.start()
        tasks = [asyncio.create_task(coro) for coro in self._background_tasks()]
        _install_shutdown(self._shutdown)
        try:
            await self._stop.wait()
        finally:
            await self._teardown(tasks, startables)

    def _background_tasks(self) -> list:
        s = self._services
        fsync_secs = self._durability.fsync_interval_ms / 1000.0
        coros = [
            self._adapter.run(),
            _run_periodically(fsync_secs, self._stop, s.writer.commit),
            _run_periodically(SNAPSHOT_INTERVAL_SECS, self._stop, self._snapshot_if_changed),
            _run_periodically(STALENESS_POLL_INTERVAL_SECS, self._stop, s.monitor.check),
        ]
        if s.fill_reconciler is not None:
            coros.append(s.fill_reconciler.run(self._stop))
        return coros

    async def _teardown(self, tasks: list, startables: list) -> None:
        self._adapter.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        snap.write_snapshot(self._durability.snapshot_path, self._services.store.state)
        self._services.writer.close()
        self._adapter.close()
        for service in reversed(startables):
            service.stop()

    def _shutdown(self) -> None:
        self._stop.set()
        self._adapter.stop()

    def _snapshot_if_changed(self) -> None:
        state = self._services.store.state
        if state.last_seq != self._last_snapshot_seq:
            snap.write_snapshot(self._durability.snapshot_path, state)
            self._last_snapshot_seq = state.last_seq


class ReplicaApplication:
    """Warm standby: tails the primary's log file and folds the same events.
    Does not connect to the exchange for market data - only reads the shared log."""

    def __init__(
        self, durability: DurabilityConfig, adapter: ExchangeAdapter, state: State, cold_path_server: ColdPathServer
    ) -> None:
        self._durability = durability
        self._adapter = adapter
        self._state = state
        self._cold_path_server = cold_path_server
        self._stop = asyncio.Event()

    async def run(self) -> None:
        await self._wait_for_log_file()
        self._cold_path_server.start()
        replica = ReplicaTail(self._durability.event_log_path, self._state)
        _install_shutdown(self._stop.set)
        tail_task = asyncio.create_task(run_replica_tail_loop(replica, self._stop))
        try:
            await self._stop.wait()
        finally:
            await self._teardown(tail_task, replica)

    async def _teardown(self, tail_task: asyncio.Task, replica: ReplicaTail) -> None:
        tail_task.cancel()
        await asyncio.gather(tail_task, return_exceptions=True)
        replica.close()
        self._adapter.close()
        self._cold_path_server.stop()

    async def _wait_for_log_file(self) -> None:
        path = self._durability.event_log_path
        logged = False
        while not os.path.exists(path):
            if not logged:
                logger.info("replica waiting for primary to create log file at %s", path)
                logged = True
            await asyncio.sleep(LOG_WAIT_POLL_SECS)
