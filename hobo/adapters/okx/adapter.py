"""OkxAdapter: composes the OKX capabilities (reference / market_data / execution
/ account) and drives the WS lifecycle. `run` gathers whichever WS clients were
created (public when a market-data stream exists; private when execution does).
"""

from __future__ import annotations

import asyncio

from hobo.adapters.base import AccountData, ExchangeAdapter, MarketData, ReferenceData
from hobo.adapters.okx import constants as c
from hobo.adapters.okx.account import OkxAccountData
from hobo.adapters.okx.execution import OkxExecutionClient
from hobo.adapters.okx.market_data import OkxMarketData
from hobo.adapters.okx.reference import OkxReferenceData
from hobo.adapters.okx.rest import OkxRestClient
from hobo.adapters.okx.websocket import OkxPrivateClient
from hobo.config import ExchangeConfig
from hobo.execution.base import ExecutionClient


class OkxAdapter(ExchangeAdapter):
    def __init__(self, config: ExchangeConfig) -> None:
        self._config = config
        self._rest = OkxRestClient(
            config.rest_url, config.api_key, config.api_secret, config.api_passphrase, config.demo_trading
        )
        self._ws_clients: list = []  # WS clients to run, populated as market_data/execution are wired
        self._reference = OkxReferenceData(self._rest)
        self._market_data = OkxMarketData(self._rest, config.ws_url, self._ws_clients.append)
        self._account = OkxAccountData(self._rest) if config.api_key else None

    def reference(self) -> ReferenceData:
        return self._reference

    def market_data(self) -> MarketData:
        return self._market_data  # bus-independent; .stream(bus, ids) wires the WS

    def account(self) -> AccountData | None:
        return self._account  # None when there are no credentials (paper)

    def execution(self) -> ExecutionClient:
        private = OkxPrivateClient(
            self._config.ws_private_url, self._config.api_key, self._config.api_secret, self._config.api_passphrase
        )
        self._ws_clients.append(private)
        return OkxExecutionClient(private, inst_id_codes=self._fetch_inst_id_codes())

    def _fetch_inst_id_codes(self) -> dict[str, int]:
        """Resolve OKX instId -> instIdCode for WS order ops (OKX deprecating instId).
        Required: WS order ops are rejected without the code, so a missing one fails startup."""
        resp = self._rest.get_instruments(c.INST_TYPE_SWAP)  # all swaps in one call
        codes = {row["instId"]: int(row["instIdCode"]) for row in resp.get("data", []) if row.get("instIdCode") is not None}
        missing = [iid for iid in self._config.instrument_ids if iid not in codes]
        if missing:
            raise RuntimeError(f"no instIdCode for {missing} from /public/instruments; OKX WS order ops require it")
        return codes

    async def run(self) -> None:
        await asyncio.gather(*(client.run() for client in self._ws_clients))

    def stop(self) -> None:
        for client in self._ws_clients:
            client.stop()

    def close(self) -> None:
        self._rest.close()
