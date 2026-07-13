"""Shadow replica: tails the primary's log file (a shared data directory) and folds
the same committed events into its own in-memory state, staying warm for manual
promotion. Polls rather than using inotify; trusts only `committed_offset`, never
scan-ahead, so it can never get ahead of what a primary restart would see as durable.
"""

from __future__ import annotations

import asyncio
import logging

from hobo.log import format as fmt
from hobo.log import reader
from hobo.risk.fold import apply_event
from hobo.risk.state import State

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 0.02  # 20ms


class ReplicaTail:
    def __init__(self, log_path: str, state: State) -> None:
        self.log_path = log_path
        self.state = state
        self._mm = reader.open_readonly(log_path)
        self._folded_offset = 0  # data-region-relative offset already folded into `state`

    def poll_once(self) -> int:
        """Fold any newly committed events. Returns how many events were folded."""
        header = reader.read_header(self._mm)
        if header.committed_offset <= self._folded_offset:
            return 0

        required_size = fmt.HEADER_SIZE + header.committed_offset
        if required_size > len(self._mm):
            self._remap()

        new_events = reader.replay_range(self._mm, self._folded_offset, header.committed_offset)
        for envelope in new_events:
            apply_event(self.state, envelope)
        self._folded_offset = header.committed_offset
        return len(new_events)

    @property
    def lag_bytes(self) -> int:
        header = reader.read_header(self._mm)
        return header.committed_offset - self._folded_offset

    def close(self) -> None:
        self._mm.close()

    def _remap(self) -> None:
        self._mm.close()
        self._mm = reader.open_readonly(self.log_path)


async def run_replica_tail_loop(
    replica: ReplicaTail,
    stop_event: asyncio.Event,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> None:
    while not stop_event.is_set():
        try:
            replica.poll_once()
        except Exception:
            logger.exception("replica tail poll failed")
        await asyncio.sleep(poll_interval_s)
