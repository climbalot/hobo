from __future__ import annotations

import dataclasses

from hobo.adapters.base import MarketData, ReferenceData
from hobo.log.events import Envelope, EventType
from hobo.log.recovery import MARK_STALENESS_THRESHOLD_NS, recover_state
from hobo.log.writer import LogWriter
from hobo.risk.fold import apply_event
from hobo.risk.model import ContractSpec, Instrument

from conftest import INSTRUMENT_ID, fill, make_state, mark_update


def spec_of(instrument: Instrument) -> ContractSpec:
    return ContractSpec(**{f.name: getattr(instrument, f.name) for f in dataclasses.fields(ContractSpec)})


class FakeReference(ReferenceData):
    def __init__(self, spec: ContractSpec) -> None:
        self._spec = spec

    def fetch_contract_spec(self, instrument_id: str) -> ContractSpec:
        return self._spec


class FakeMarket(MarketData):
    def __init__(self, price: float, ts_ns: int) -> None:
        self._price = price
        self._ts_ns = ts_ns

    def fetch_mark(self, instrument_id: str) -> tuple[float, int]:
        return self._price, self._ts_ns

    def stream(self, bus, instrument_ids) -> None:  # pragma: no cover - not exercised here
        raise NotImplementedError


def factory(instrument):
    return lambda: make_state(instrument)


def test_recovery_with_no_log_or_snapshot_uses_fresh_state(tmp_path, instrument):
    log_path = str(tmp_path / "eventlog.bin")
    LogWriter(log_path).close()

    result = recover_state(log_path, str(tmp_path / "snapshots"), factory(instrument))
    assert result.recovered_from_snapshot is False
    assert result.replayed_event_count == 0
    assert result.state.last_seq == 0


def test_recovery_replays_full_log_when_no_snapshot(tmp_path, instrument):
    log_path = str(tmp_path / "eventlog.bin")
    writer = LogWriter(log_path)
    writer.append(EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 50_000, 1), ts_ns=1)
    writer.append(EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000), ts_ns=2)
    writer.close()

    result = recover_state(log_path, str(tmp_path / "snapshots"), factory(instrument))
    assert result.recovered_from_snapshot is False
    assert result.replayed_event_count == 2
    assert result.state.desk.books["A"].position(INSTRUMENT_ID).qty == 10


def test_recovery_uses_snapshot_and_replays_only_the_tail(tmp_path, instrument):
    from hobo.log import snapshot as snap

    log_path = str(tmp_path / "eventlog.bin")
    snapshot_dir = str(tmp_path / "snapshots")

    writer = LogWriter(log_path)
    writer.append(EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 50_000, 1), ts_ns=1)
    writer.append(EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000), ts_ns=2)
    writer.commit()

    state_after_two = make_state(instrument)
    for env in [
        Envelope(1, 1, EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 50_000, 1)),
        Envelope(2, 2, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000)),
    ]:
        apply_event(state_after_two, env)
    snap.write_snapshot(snapshot_dir, state_after_two)

    writer.append(EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 51_000, 3), ts_ns=3)
    writer.close()

    result = recover_state(log_path, snapshot_dir, factory(instrument))
    assert result.recovered_from_snapshot is True
    assert result.replayed_event_count == 1  # only the post-snapshot tail
    assert result.state.mark(INSTRUMENT_ID) == 51_000
    assert result.state.desk.books["A"].position(INSTRUMENT_ID).qty == 10


def _log_with_one_mark(tmp_path):
    log_path = str(tmp_path / "eventlog.bin")
    writer = LogWriter(log_path)
    writer.append(EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 50_000, 1), ts_ns=1)
    writer.close()
    return log_path


def test_reconciliation_clean_case_produces_no_warnings(tmp_path, instrument):
    log_path = _log_with_one_mark(tmp_path)
    result = recover_state(
        log_path,
        str(tmp_path / "snapshots"),
        factory(instrument),
        reference_client=FakeReference(spec_of(instrument)),
        market_client=FakeMarket(50_000, 1),
        now_ns=1,
    )
    assert result.warnings == []


def test_reconciliation_detects_instrument_spec_drift(tmp_path, instrument):
    log_path = _log_with_one_mark(tmp_path)
    drifted = dataclasses.replace(spec_of(instrument), tick_sz=0.5)

    result = recover_state(
        log_path,
        str(tmp_path / "snapshots"),
        factory(instrument),
        reference_client=FakeReference(drifted),
        now_ns=1,
    )
    assert len(result.warnings) == 1
    assert result.warnings[0].kind == "INSTRUMENT_SPEC_DRIFT"
    assert result.warnings[0].detail["diff"]["tick_sz"] == (0.1, 0.5)
    assert result.state.instruments[INSTRUMENT_ID].tick_sz == 0.5


def test_reconciliation_detects_stale_mark_and_seeds_fresh_value(tmp_path, instrument):
    log_path = _log_with_one_mark(tmp_path)
    now = 1 + MARK_STALENESS_THRESHOLD_NS + 1

    result = recover_state(
        log_path,
        str(tmp_path / "snapshots"),
        factory(instrument),
        market_client=FakeMarket(52_000, now),
        now_ns=now,
    )
    assert len(result.warnings) == 1
    assert result.warnings[0].kind == "MARK_STALE"
    assert result.state.mark(INSTRUMENT_ID) == 52_000


def test_reconciliation_fresh_mark_within_threshold_produces_no_warning(tmp_path, instrument):
    log_path = _log_with_one_mark(tmp_path)
    now = 1 + MARK_STALENESS_THRESHOLD_NS - 1

    result = recover_state(
        log_path,
        str(tmp_path / "snapshots"),
        factory(instrument),
        market_client=FakeMarket(50_000, now),
        now_ns=now,
    )
    assert result.warnings == []
    assert result.state.mark(INSTRUMENT_ID) == 50_000  # not overwritten
