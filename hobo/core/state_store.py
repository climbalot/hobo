"""The one place risk state and the durable log live together. `record(event)`
appends a typed log payload then folds it into state - the single path for any
state/audit mutation. Readers use `.state`.
"""

from __future__ import annotations

import time

from hobo.log.events import Envelope, LogEvent
from hobo.log.writer import LogWriter
from hobo.risk.fold import apply_event
from hobo.risk.state import State


class StateStore:
    def __init__(self, state: State, writer: LogWriter) -> None:
        self.state = state
        self._writer = writer

    def record(self, event: LogEvent) -> Envelope:
        envelope = self._writer.append(event.event_type, event.to_dict(), time.time_ns())
        apply_event(self.state, envelope)
        return envelope
