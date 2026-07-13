"""Internal paper-fill execution client (the offline/CI/no-keys default): instant,
full fill at the current mark, so cancel is a no-op (nothing ever rests).
"""

from __future__ import annotations

from hobo.execution.base import ExecutionClient, FillData
from hobo.risk.model import Order


class PaperExecutionClient(ExecutionClient):
    def submit(self, order: Order, mark_price: float) -> None:
        self._emit_fill(
            FillData(
                order_id=order.order_id,
                book_id=order.book_id,
                instrument_id=order.instrument_id,
                side=order.side.value,
                qty=order.qty,
                fill_price=mark_price,
            )
        )

    def cancel(self, order_id: str) -> None:
        return  # nothing rests: paper orders fill instantly on submit
