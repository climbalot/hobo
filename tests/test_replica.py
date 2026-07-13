from __future__ import annotations

from hobo.log.events import EventType
from hobo.log.writer import LogWriter
from hobo.replica.tail import ReplicaTail
from hobo.risk.fold import apply_event

from conftest import INSTRUMENT_ID, fill, make_state, mark_update


def test_replica_converges_to_primary_state_after_polling(tmp_path, instrument):
    log_path = str(tmp_path / "eventlog.bin")
    primary_writer = LogWriter(log_path)
    primary_state = make_state(instrument)

    def commit_event(event_type, payload, ts_ns):
        env = primary_writer.append(event_type, payload, ts_ns)
        apply_event(primary_state, env)
        primary_writer.commit()

    commit_event(EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 50_000, 1), 1)
    commit_event(EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000), 2)

    replica_state = make_state(instrument)
    replica = ReplicaTail(log_path, replica_state)
    folded = replica.poll_once()

    assert folded == 2
    assert replica.lag_bytes == 0
    assert replica_state.to_dict() == primary_state.to_dict()

    commit_event(EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 51_000, 3), 3)
    folded = replica.poll_once()
    assert folded == 1
    assert replica_state.to_dict() == primary_state.to_dict()

    primary_writer.close()
    replica.close()


def test_replica_only_folds_committed_events(tmp_path, instrument):
    log_path = str(tmp_path / "eventlog.bin")
    primary_writer = LogWriter(log_path)
    primary_writer.append(EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 50_000, 1), ts_ns=1)
    primary_writer.commit()
    primary_writer.append(EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 51_000, 2), ts_ns=2)
    # second event not committed yet

    replica_state = make_state(instrument)
    replica = ReplicaTail(log_path, replica_state)
    folded = replica.poll_once()

    assert folded == 1
    assert replica_state.mark(INSTRUMENT_ID) == 50_000  # the uncommitted tail is not visible

    primary_writer.commit()
    folded = replica.poll_once()
    assert folded == 1
    assert replica_state.mark(INSTRUMENT_ID) == 51_000

    primary_writer.close()
    replica.close()


def test_replica_survives_log_growth_across_polls(tmp_path, instrument):
    log_path = str(tmp_path / "eventlog.bin")
    primary_writer = LogWriter(log_path, initial_capacity=64)
    primary_state = make_state(instrument)

    replica_state = make_state(instrument)
    replica = ReplicaTail(log_path, replica_state)

    for i in range(1, 101):
        env = primary_writer.append(EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 50_000 + i, i), ts_ns=i)
        apply_event(primary_state, env)
        primary_writer.commit()
        replica.poll_once()

    assert primary_writer.capacity > 64  # confirm growth actually happened during the run
    assert replica_state.to_dict() == primary_state.to_dict()

    primary_writer.close()
    replica.close()
