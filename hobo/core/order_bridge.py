"""Manual order entry from the dashboard, bridged from the HTTP server thread onto
the engine loop: `place()` marshals via `run_coroutine_threadsafe` and blocks for
the GateDecision, so the fold/log stay single-threaded with the strategy path.
"""

from __future__ import annotations

import asyncio

from hobo.core.gateway import OrderGateway
from hobo.risk.model import Side


class OrderBridge:
    def __init__(self, gateway: OrderGateway) -> None:
        self._gateway = gateway
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop  # set once the engine loop is running (Application.run)

    def place(self, book_id: str, instrument_id: str, side: str, qty: float) -> dict:
        if self._loop is None:
            return {"ok": False, "error": "engine not running"}
        try:
            side_enum = Side[side.upper()]
        except KeyError:
            return {"ok": False, "error": f"invalid side {side!r} (BUY/SELL)"}
        if qty <= 0:
            return {"ok": False, "error": "qty must be > 0"}
        try:
            future = asyncio.run_coroutine_threadsafe(self._place(book_id, instrument_id, side_enum, qty), self._loop)
            decision = future.result(timeout=5)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}
        return {
            "ok": True,
            "approved": decision.approved,
            "order_id": decision.order_id,
            "reason": decision.reason.value,
            "detail": decision.detail,
        }

    async def _place(self, book_id: str, instrument_id: str, side: Side, qty: float):
        return self._gateway.place(book_id, instrument_id, side, qty)
