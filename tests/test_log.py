from __future__ import annotations

import os
import struct

import pytest

from hobo.log import format as fmt
from hobo.log import reader
from hobo.log.events import EventType
from hobo.log.writer import LogWriter

from conftest import mark_update


@pytest.fixture
def log_path(tmp_path):
    return str(tmp_path / "eventlog.bin")


def test_append_and_replay_round_trip(log_path):
    writer = LogWriter(log_path, initial_capacity=4096)
    envelopes = []
    for i in range(1, 21):
        envelopes.append(writer.append(EventType.MARK_UPDATE, mark_update("BTC-USDT-SWAP", 50_000 + i, i), ts_ns=i))
    writer.close()

    mm = reader.open_readonly(log_path)
    replayed = reader.replay(mm)
    assert [e.to_json_dict() for e in replayed] == [e.to_json_dict() for e in envelopes]


def test_replay_empty_log_before_any_commit(log_path):
    writer = LogWriter(log_path, initial_capacity=4096)
    writer.append(EventType.MARK_UPDATE, mark_update("BTC-USDT-SWAP", 50_000, 1), ts_ns=1)
    # deliberately do NOT commit - nothing is durable yet

    mm = reader.open_readonly(log_path)
    assert reader.replay(mm) == []


def test_forced_multi_growth_survives_replay(log_path):
    # Tiny initial capacity forces several doublings well before 200 frames fit.
    writer = LogWriter(log_path, initial_capacity=64, growth_factor=2)
    envelopes = []
    for i in range(1, 201):
        envelopes.append(writer.append(EventType.MARK_UPDATE, mark_update("BTC-USDT-SWAP", 50_000 + i, i), ts_ns=i))
    assert writer.capacity > 64  # confirm growth actually happened
    writer.close()

    mm = reader.open_readonly(log_path)
    replayed = reader.replay(mm)
    assert [e.to_json_dict() for e in replayed] == [e.to_json_dict() for e in envelopes]


def test_reopen_recovers_uncommitted_whole_frames(log_path):
    writer = LogWriter(log_path, initial_capacity=4096)
    e1 = writer.append(EventType.MARK_UPDATE, mark_update("BTC-USDT-SWAP", 50_000, 1), ts_ns=1)
    e2 = writer.append(EventType.MARK_UPDATE, mark_update("BTC-USDT-SWAP", 50_001, 2), ts_ns=2)
    # No commit() call - simulates a bare process crash before the next fsync batch.
    # The bytes are still in the mmap (~page cache), so a fresh writer should recover them.
    writer._mm.close()
    os.close(writer._fd)

    recovered_writer = LogWriter(log_path)
    assert recovered_writer.recovered_uncommitted_count == 2
    assert recovered_writer.last_seq == 2
    assert recovered_writer.committed_offset == recovered_writer.write_offset  # auto-committed on recovery

    mm = reader.open_readonly(log_path)
    replayed = reader.replay(mm)
    assert [e.seq for e in replayed] == [e1.seq, e2.seq]
    recovered_writer.close()


def test_committed_offset_only_replay_ignores_uncommitted_tail(log_path):
    writer = LogWriter(log_path, initial_capacity=4096)
    writer.append(EventType.MARK_UPDATE, mark_update("BTC-USDT-SWAP", 50_000, 1), ts_ns=1)
    writer.commit()
    writer.append(EventType.MARK_UPDATE, mark_update("BTC-USDT-SWAP", 50_001, 2), ts_ns=2)
    # second event not committed

    mm = reader.open_readonly(log_path)
    replayed = reader.replay(mm)
    assert len(replayed) == 1
    assert replayed[0].payload["mark_price"] == 50_000
    writer.close()


def test_torn_write_stops_cleanly_without_exception(log_path):
    writer = LogWriter(log_path, initial_capacity=4096)
    good = writer.append(EventType.MARK_UPDATE, mark_update("BTC-USDT-SWAP", 50_000, 1), ts_ns=1)
    writer.commit()

    # Simulate a torn write: a valid length prefix claiming a large payload, but only
    # part of the payload bytes actually landed (the rest is the pre-zeroed region).
    offset = fmt.HEADER_SIZE + writer.write_offset
    struct.pack_into("<I", writer._mm, offset, 500)
    partial_payload = b'{"seq":2,"ts_ns":2,"event_type":"MARK_UPDATE","payload":{"in'
    writer._mm[offset + fmt.FRAME_LEN_SIZE : offset + fmt.FRAME_LEN_SIZE + len(partial_payload)] = partial_payload
    writer._mm.flush()

    new_offset, new_seq, count = fmt.scan_ahead(writer._mm, writer.committed_offset, writer.capacity, writer.last_seq)
    assert count == 0  # the torn frame is not recovered
    assert new_offset == writer.committed_offset  # offset unchanged - stopped at the torn frame
    assert new_seq == good.seq

    mm = reader.open_readonly(log_path)
    replayed = reader.replay(mm)
    assert [e.seq for e in replayed] == [good.seq]
    writer.close()


def test_bad_magic_raises_corrupt_log_error(tmp_path):
    path = tmp_path / "not_a_log.bin"
    path.write_bytes(b"garbage" * 1000)
    with pytest.raises(fmt.CorruptLogError):
        fmt.unpack_header(reader.open_readonly(str(path)))
