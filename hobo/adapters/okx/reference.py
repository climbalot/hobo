"""OKX reference-data capability: a raw OKX instruments response -> domain
ContractSpec. The seam between the OKX REST client (no domain knowledge) and the
engine. Contract spec carries no risk params - the maintenance-margin rate is
attached later at assembly (a config constant). Marks are market data (market_data.py).
"""

from __future__ import annotations

from hobo.adapters.base import ReferenceData
from hobo.adapters.okx import constants as c
from hobo.adapters.okx.rest import OkxRestClient
from hobo.risk.model import ContractSpec


class ReferenceDataError(RuntimeError):
    pass


def parse_contract_spec(response: dict, instrument_id: str) -> ContractSpec:
    match = next((e for e in response.get("data") or [] if e.get("instId") == instrument_id), None)
    if match is None:
        raise ReferenceDataError(f"instrument {instrument_id!r} not found in OKX response")
    try:
        return ContractSpec(
            instrument_id=match["instId"],
            ct_val=float(match["ctVal"]),
            ct_val_ccy=match["ctValCcy"],
            ct_mult=float(match["ctMult"]),
            tick_sz=float(match["tickSz"]),
            lot_sz=float(match["lotSz"]),
            min_sz=float(match["minSz"]),
            max_leverage=float(match["lever"]),
            settle_ccy=match["settleCcy"],
            contract_type=match.get("ctType", "linear"),
        )
    except (KeyError, ValueError) as exc:
        raise ReferenceDataError(f"malformed instrument spec for {instrument_id!r}: {exc}") from exc


class OkxReferenceData(ReferenceData):
    def __init__(self, rest: OkxRestClient, inst_type: str = c.INST_TYPE_SWAP) -> None:
        self._rest = rest
        self._inst_type = inst_type

    def fetch_contract_spec(self, instrument_id: str) -> ContractSpec:
        return parse_contract_spec(self._rest.get_instruments(self._inst_type, instrument_id), instrument_id)
