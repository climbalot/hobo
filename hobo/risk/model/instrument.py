"""Instrument model: the exchange contract spec and the risk view of it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContractSpec:
    """Pure exchange contract spec for one instrument - what an adapter returns.
    No risk parameters (see Instrument)."""

    instrument_id: str
    ct_val: float
    ct_val_ccy: str
    ct_mult: float
    tick_sz: float
    lot_sz: float
    min_sz: float
    max_leverage: float
    settle_ccy: str
    contract_type: str = "linear"


@dataclass(frozen=True)
class Instrument:
    """Contract spec plus risk parameters - the risk core's view of an instrument.
    Assembled from an exchange ContractSpec and risk config via `from_spec`."""

    instrument_id: str
    ct_val: float
    ct_val_ccy: str
    ct_mult: float
    tick_sz: float
    lot_sz: float
    min_sz: float
    max_leverage: float
    settle_ccy: str
    maintenance_margin_rate: float
    contract_type: str = "linear"

    @property
    def multiplier(self) -> float:
        """One contract's face value in settlement currency, before price."""
        return self.ct_val * self.ct_mult

    @classmethod
    def from_spec(cls, spec: ContractSpec, maintenance_margin_rate: float) -> "Instrument":
        return cls(
            **{f: getattr(spec, f) for f in spec.__dataclass_fields__},
            maintenance_margin_rate=maintenance_margin_rate,
        )
