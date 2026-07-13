from __future__ import annotations

import os

from hobo.log import snapshot as snap
from hobo.log.events import Envelope, EventType
from hobo.risk.fold import apply_event
from hobo.risk.state import State

from conftest import INSTRUMENT_ID, fill, funding_update, make_state, mark_update


def apply_sequence(state: State, sequence: list[Envelope]) -> State:
    for env in sequence:
        apply_event(state, env)
    return state


def build_sequence() -> list[Envelope]:
    return [
        Envelope(1, 1, EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 50_000, 1)),
        Envelope(2, 2, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000)),
        Envelope(3, 3, EventType.FILL, fill("o2", "B", INSTRUMENT_ID, "SELL", 5, 50_000)),
        Envelope(4, 4, EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 51_500, 4)),
        Envelope(5, 5, EventType.FUNDING_UPDATE, funding_update(INSTRUMENT_ID, 0.0005, 5000)),
        Envelope(6, 6, EventType.FILL, fill("o3", "A", INSTRUMENT_ID, "SELL", 3, 51_500)),
    ]


def test_snapshot_round_trip(tmp_path, instrument):
    state = apply_sequence(make_state(instrument), build_sequence())

    path = snap.write_snapshot(str(tmp_path), state)
    assert os.path.exists(path)

    loaded = snap.load_latest_state(str(tmp_path))
    assert loaded.to_dict() == state.to_dict()


def test_load_latest_state_none_when_no_snapshots(tmp_path):
    assert snap.load_latest_state(str(tmp_path / "does_not_exist")) is None


def test_retention_keeps_only_last_k(tmp_path, instrument):
    state = make_state(instrument)
    for seq in range(1, 8):
        state.last_seq = seq
        snap.write_snapshot(str(tmp_path), state, keep_last=3)

    remaining = sorted(f for f in os.listdir(tmp_path) if f.startswith("snapshot-"))
    assert remaining == ["snapshot-5.json", "snapshot-6.json", "snapshot-7.json"]


def test_snapshot_plus_tail_replay_equals_full_replay(tmp_path, instrument):
    sequence = build_sequence()
    split = 3  # snapshot after the first 3 events, replay the rest as the "tail"

    full_replay_state = apply_sequence(make_state(instrument), sequence)

    snapshotted_state = apply_sequence(make_state(instrument), sequence[:split])
    snap.write_snapshot(str(tmp_path), snapshotted_state)

    recovered_state = snap.load_latest_state(str(tmp_path))
    apply_sequence(recovered_state, sequence[split:])

    assert recovered_state.to_dict() == full_replay_state.to_dict()
