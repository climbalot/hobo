"""The event-log schema: one typed payload per event type. Each dataclass IS the
persisted payload (tagged by `event_type`), serialized to JSON via `to_dict()`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import ClassVar


class EventType:
    MARK_UPDATE = "MARK_UPDATE"
    FUNDING_UPDATE = "FUNDING_UPDATE"
    ORDER_REQUEST = "ORDER_REQUEST"
    GATE_DECISION = "GATE_DECISION"
    FILL = "FILL"
    LIMIT_CHANGE = "LIMIT_CHANGE"
    KILL_SWITCH = "KILL_SWITCH"
    RECONCILIATION_WARNING = "RECONCILIATION_WARNING"


STATE_MUTATING_EVENT_TYPES = frozenset(
    {
        EventType.MARK_UPDATE,
        EventType.FUNDING_UPDATE,
        EventType.FILL,
        EventType.LIMIT_CHANGE,
        EventType.KILL_SWITCH,
    }
)


@dataclass(frozen=True)
class Envelope:
    seq: int
    ts_ns: int
    event_type: str
    payload: dict

    def to_json_dict(self) -> dict:
        return {"seq": self.seq, "ts_ns": self.ts_ns, "event_type": self.event_type, "payload": self.payload}

    @classmethod
    def from_json_dict(cls, d: dict) -> "Envelope":
        return cls(seq=d["seq"], ts_ns=d["ts_ns"], event_type=d["event_type"], payload=d["payload"])


@dataclass(frozen=True)
class LogEvent:
    """Base for a logged event payload. `event_type` tags it; `to_dict` is the
    persisted payload (all dataclass fields, excluding the ClassVar tag)."""

    event_type: ClassVar[str] = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# --- state-mutating events (folded on replay) ---


@dataclass(frozen=True)
class MarkUpdate(LogEvent):
    event_type: ClassVar[str] = EventType.MARK_UPDATE
    instrument_id: str
    mark_price: float
    ts_exch_ns: int


@dataclass(frozen=True)
class FundingUpdate(LogEvent):
    event_type: ClassVar[str] = EventType.FUNDING_UPDATE
    instrument_id: str
    funding_rate: float
    funding_time_ns: int


@dataclass(frozen=True)
class Fill(LogEvent):
    event_type: ClassVar[str] = EventType.FILL
    order_id: str
    book_id: str
    instrument_id: str
    side: str
    qty: float
    fill_price: float
    fee: float = 0.0
    trade_id: str = ""


@dataclass(frozen=True)
class LimitChange(LogEvent):
    event_type: ClassVar[str] = EventType.LIMIT_CHANGE
    scope: str  # "BOOK" | "DESK"
    field: str
    old: float
    new: float
    book_id: str | None = None


@dataclass(frozen=True)
class KillSwitch(LogEvent):
    event_type: ClassVar[str] = EventType.KILL_SWITCH
    scope: str  # "BOOK" | "DESK"
    id: str
    enabled: bool
    reason: str = ""


# --- audit-only events (logged, not folded) ---


@dataclass(frozen=True)
class OrderRequest(LogEvent):
    event_type: ClassVar[str] = EventType.ORDER_REQUEST
    order_id: str
    book_id: str
    instrument_id: str
    side: str
    qty: float
    order_type: str = "MARKET"


@dataclass(frozen=True)
class GateDecisionLog(LogEvent):
    event_type: ClassVar[str] = EventType.GATE_DECISION
    order_id: str
    book_id: str
    approved: bool
    reason: str
    detail: dict


@dataclass(frozen=True)
class ReconciliationWarning(LogEvent):
    event_type: ClassVar[str] = EventType.RECONCILIATION_WARNING
    kind: str
    detail: dict
