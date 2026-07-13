"""OKX execution capability: approved orders go out as market orders over the
private WS; fills arrive async on the orders channel, mapped back via a clOrdId
table (we mint our own - OKX clOrdId is alphanumeric-only). OKX is deprecating
`instId` for the numeric `instIdCode`, so order ops route through `_instrument_ref`.
"""

from __future__ import annotations

import asyncio

from hobo.adapters.okx import constants as c
from hobo.adapters.okx.websocket import OkxPrivateClient
from hobo.execution.base import ExecutionClient, FillData, OrderUpdate
from hobo.risk.model import Order


class OkxExecutionClient(ExecutionClient):
    def __init__(
        self,
        private_client: OkxPrivateClient,
        inst_id_codes: dict[str, int] | None = None,
        td_mode: str = c.DEFAULT_TD_MODE,
    ) -> None:
        super().__init__()
        self._client = private_client
        self._td_mode = td_mode
        # OKX instId -> instIdCode, resolved by the adapter; default-empty only for test construction (instId only).
        self._inst_id_codes = inst_id_codes or {}
        self._orders_by_clordid: dict[str, Order] = {}
        self._clordid_by_order_id: dict[str, str] = {}
        self._seq = 0
        private_client.subscribe_private({"channel": c.ORDERS_CHANNEL, "instType": c.INST_TYPE_SWAP})
        private_client.on(c.ORDERS_CHANNEL, self._on_order_update)

    def _instrument_ref(self, instrument_id: str) -> dict:
        """Instrument identity for a WS order op: instIdCode (post-deprecation) plus
        instId alongside while both are accepted; instId only if the code is unknown."""
        ref: dict = {"instId": instrument_id}
        inst_id_code = self._inst_id_codes.get(instrument_id)
        if inst_id_code is not None:
            ref["instIdCode"] = inst_id_code
        return ref

    def submit(self, order: Order, mark_price: float) -> None:
        self._seq += 1
        cl_ord_id = f"{order.book_id}{self._seq:010d}"
        self._orders_by_clordid[cl_ord_id] = order
        self._clordid_by_order_id[order.order_id] = cl_ord_id
        args = {
            **self._instrument_ref(order.instrument_id),
            "side": order.side.value.lower(),
            "tdMode": self._td_mode,
            "ordType": c.ORDER_TYPE_MARKET,
            "sz": str(order.qty),
            "clOrdId": cl_ord_id,
        }
        asyncio.ensure_future(self._client.place_order(args))

    def cancel(self, order_id: str) -> None:
        cl_ord_id = self._clordid_by_order_id.get(order_id)
        if cl_ord_id is None:
            return
        order = self._orders_by_clordid[cl_ord_id]
        asyncio.ensure_future(self._client.cancel_order({**self._instrument_ref(order.instrument_id), "clOrdId": cl_ord_id}))

    def _on_order_update(self, msg) -> None:
        entry = msg.data
        order = self._orders_by_clordid.get(entry.get("clOrdId"))
        if order is None:
            return

        fill_sz = float(entry.get("fillSz") or 0)
        if fill_sz > 0:
            self._emit_fill(
                FillData(
                    order_id=order.order_id,
                    book_id=order.book_id,
                    instrument_id=order.instrument_id,
                    side=order.side.value,
                    qty=fill_sz,
                    fill_price=float(entry["fillPx"]),
                    fee=-float(entry.get("fillFee") or 0),  # OKX fillFee negative when charged
                    trade_id=entry.get("tradeId", ""),
                )
            )

        if entry.get("state") == c.STATE_CANCELED:
            self._emit_order_update(OrderUpdate(order.order_id, order.book_id, "CANCELED"))
