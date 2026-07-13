"""Strategy interface: receives typed events (market data and its own fills) and
returns book-agnostic action intents (place/cancel). Book-agnostic and a pure
function of (event, state) - the StrategyRunner pairs it with a book.
"""

from __future__ import annotations

from typing import Protocol

from hobo.core.events import Action, Event
from hobo.risk.state import State


class Strategy(Protocol):
    def on_event(self, event: Event, state: State) -> list[Action]: ...
