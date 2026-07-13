"""In-memory synchronous event bus: typed publish/subscribe with a single-consumer
FIFO drain - a re-entrancy guard queues events published mid-drain (e.g. a fill
emitted mid-tick) and delivers them in order, never nested. A raising handler is
logged and skipped; past `max_queue_size` events are dropped loudly (safety valve
against a runaway cascade, not normal load - depth is ~1 in steady state).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable

from hobo.core.events import Event

logger = logging.getLogger(__name__)

Handler = Callable[[Event], None]
DEFAULT_MAX_QUEUE_SIZE = 10_000


class EventBus:
    def __init__(
        self,
        max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE,
        on_dispatch_ns: Callable[[type, int], None] | None = None,
        on_dropped: Callable[[type], None] | None = None,
    ) -> None:
        self._handlers: dict[type, list[Handler]] = {}
        self._queue: deque[Event] = deque()
        self._draining = False
        self._max_queue_size = max_queue_size
        self._on_dispatch_ns = on_dispatch_ns
        self._on_dropped = on_dropped
        self.dropped = 0

    def subscribe(self, event_type: type, handler: Handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def publish(self, event: Event) -> None:
        if len(self._queue) >= self._max_queue_size:
            self.dropped += 1
            logger.warning(
                "event bus queue full, dropping event",
                extra={"event_type": type(event).__name__, "max_queue_size": self._max_queue_size},
            )
            if self._on_dropped is not None:
                self._on_dropped(type(event))
            return

        self._queue.append(event)
        if self._draining:
            return
        self._draining = True
        try:
            while self._queue:
                self._deliver(self._queue.popleft())
        finally:
            self._draining = False

    def _deliver(self, event: Event) -> None:
        handlers = self._handlers.get(type(event))
        if not handlers:
            return
        start = time.perf_counter_ns()
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("event handler failed", extra={"event_type": type(event).__name__})
        if self._on_dispatch_ns is not None:
            self._on_dispatch_ns(type(event), time.perf_counter_ns() - start)
