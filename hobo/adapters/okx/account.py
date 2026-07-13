"""OKX account-data capability: net positions, balance, and executions (paged
toward history), each a read-only signed REST query against the authenticated
account.
"""

from __future__ import annotations

from hobo.adapters.base import AccountData, Balance, ExchangeFill
from hobo.adapters.okx import constants as c
from hobo.adapters.okx.rest import OkxRestClient


class OkxAccountData(AccountData):
    def __init__(self, rest: OkxRestClient) -> None:
        self._rest = rest

    def positions(self) -> dict[str, float]:
        positions: dict[str, float] = {}
        for row in self._rest.get_positions(c.INST_TYPE_SWAP).get("data", []):
            if row.get("pos") not in (None, ""):  # net signed qty in contracts
                positions[row["instId"]] = positions.get(row["instId"], 0.0) + float(row["pos"])
        return positions

    def balance(self) -> Balance:
        rows = self._rest.get_balance().get("data") or [{}]
        return Balance(total_equity_usdt=float(rows[0].get("totalEq") or 0.0))

    def fills(self, after: str | None = None) -> list[ExchangeFill]:
        fills = []
        for row in self._rest.get_fills(c.INST_TYPE_SWAP, after=after).get("data", []):
            if not row.get("fillSz"):
                continue
            fills.append(
                ExchangeFill(
                    trade_id=row.get("tradeId", ""),
                    client_order_id=row.get("clOrdId", ""),
                    instrument_id=row["instId"],
                    side=row["side"].upper(),
                    qty=float(row["fillSz"]),
                    price=float(row["fillPx"]),
                    fee=-float(row.get("fee") or 0),  # OKX fee negative when charged
                    ts_ns=int(row.get("ts") or 0) * 1_000_000,
                    cursor=row.get("billId", ""),
                )
            )
        return fills
