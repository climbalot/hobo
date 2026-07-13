"""ExchangeAdapter: the one interface an exchange integration implements. The
engine core depends only on this (plus domain types), never a concrete exchange
package. An adapter is a factory of four capabilities - reference, market_data,
execution, account - plus its own connection lifecycle.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from hobo.core.bus import EventBus
from hobo.execution.base import ExecutionClient
from hobo.risk.model import ContractSpec


# --- data types the capabilities return ---


@dataclass(frozen=True)
class Balance:
    """Account equity in USDT; extend with margin/free/per-ccy detail as needed."""

    total_equity_usdt: float


@dataclass(frozen=True)
class ExchangeFill:
    """A single execution reported by the exchange, for fill reconciliation:
    `trade_id` dedups against WS-ingested fills, `client_order_id` attributes it to a book."""

    trade_id: str
    client_order_id: str
    instrument_id: str
    side: str  # "BUY" | "SELL"
    qty: float
    price: float
    fee: float  # signed cost (USDT); positive = paid
    ts_ns: int
    cursor: str = ""  # exchange pagination cursor for the next-older page (OKX billId)


# --- capability interfaces: small, single-purpose, composed by an adapter ---


class ReferenceData(abc.ABC):
    """Instrument reference data: the static contract spec, via sync REST."""

    @abc.abstractmethod
    def fetch_contract_spec(self, instrument_id: str) -> ContractSpec: ...


class MarketData(abc.ABC):
    """Market data: a sync REST snapshot (`fetch_mark`, usable before the stream is
    up) and a live WS `stream` publishing MarkEvent/FundingEvent onto a bus."""

    @abc.abstractmethod
    def fetch_mark(self, instrument_id: str) -> tuple[float, int]: ...

    @abc.abstractmethod
    def stream(self, bus: EventBus, instrument_ids: list[str]) -> None: ...


class AccountData(abc.ABC):
    """Read-only account truth, one query per fact. Positions are net signed qty
    (contracts) per instrument - the exchange knows one net position per instrument,
    not our per-book split. `fills` pages toward history via `after`."""

    @abc.abstractmethod
    def positions(self) -> dict[str, float]: ...

    @abc.abstractmethod
    def balance(self) -> Balance: ...

    @abc.abstractmethod
    def fills(self, after: str | None = None) -> list[ExchangeFill]: ...


class ExchangeAdapter(abc.ABC):
    """A factory of the exchange's capabilities plus connection lifecycle."""

    @abc.abstractmethod
    def reference(self) -> ReferenceData: ...

    @abc.abstractmethod
    def market_data(self) -> MarketData: ...

    @abc.abstractmethod
    def execution(self) -> ExecutionClient: ...

    def account(self) -> AccountData | None:
        """The authenticated account, or None for paper / no credentials. Optional."""
        return None

    # lifecycle for the adapter's own connections (WS clients created above)
    @abc.abstractmethod
    async def run(self) -> None: ...

    @abc.abstractmethod
    def stop(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...
