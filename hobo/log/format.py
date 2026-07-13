"""On-disk frame format for the append-only event log: a fixed 4096-byte header
page (magic|version|capacity|committed_offset|last_seq, little-endian) then a data
region of length-prefixed JSON frames. `committed_offset` is the durability
high-water mark a power-loss-safe replay may trust; a zero length prefix (from
ftruncate zero-fill) is the end-of-data sentinel.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass

from hobo.log.events import Envelope

MAGIC = b"RISKLOG1"
VERSION = 1
HEADER_SIZE = 4096

_HEADER_STRUCT = struct.Struct("<8sIqqq")
CAPACITY_OFF = 12
COMMITTED_OFFSET_OFF = 20
LAST_SEQ_OFF = 28

FRAME_LEN_STRUCT = struct.Struct("<I")
FRAME_LEN_SIZE = FRAME_LEN_STRUCT.size


class CorruptLogError(ValueError):
    """Raised when the header itself is unreadable (bad magic/truncated file)."""


@dataclass
class HeaderFields:
    version: int
    capacity: int
    committed_offset: int
    last_seq: int


def pack_header(capacity: int, committed_offset: int, last_seq: int) -> bytes:
    buf = bytearray(HEADER_SIZE)
    _HEADER_STRUCT.pack_into(buf, 0, MAGIC, VERSION, capacity, committed_offset, last_seq)
    return bytes(buf)


def unpack_header(buf) -> HeaderFields:
    if len(buf) < HEADER_SIZE:
        raise CorruptLogError(f"file too short to contain a header: {len(buf)} bytes")
    magic, version, capacity, committed_offset, last_seq = _HEADER_STRUCT.unpack_from(buf, 0)
    if magic != MAGIC:
        raise CorruptLogError(f"not a risk-engine log file (bad magic: {magic!r})")
    return HeaderFields(version=version, capacity=capacity, committed_offset=committed_offset, last_seq=last_seq)


def write_capacity(buf, capacity: int) -> None:
    struct.pack_into("<q", buf, CAPACITY_OFF, capacity)


def write_committed(buf, committed_offset: int, last_seq: int) -> None:
    struct.pack_into("<qq", buf, COMMITTED_OFFSET_OFF, committed_offset, last_seq)


def encode_frame(envelope: Envelope) -> bytes:
    payload_bytes = json.dumps(envelope.to_json_dict(), separators=(",", ":")).encode("utf-8")
    return FRAME_LEN_STRUCT.pack(len(payload_bytes)) + payload_bytes + b"\n"


def decode_frame_at(buf, offset: int, limit: int) -> tuple[Envelope | None, int]:
    """Decode one frame at `offset`, not reading past `limit`. Returns (envelope,
    offset_after_frame), or (None, offset) if there's no valid frame here - zero-filled
    end-of-data or a torn write; callers stop on `next_offset == offset` either way.
    """
    if offset + FRAME_LEN_SIZE > limit:
        return None, offset
    (length,) = FRAME_LEN_STRUCT.unpack_from(buf, offset)
    if length <= 0:
        return None, offset
    frame_end = offset + FRAME_LEN_SIZE + length + 1
    if frame_end > limit:
        return None, offset
    payload_bytes = bytes(buf[offset + FRAME_LEN_SIZE : offset + FRAME_LEN_SIZE + length])
    trailing = bytes(buf[offset + FRAME_LEN_SIZE + length : frame_end])
    if trailing != b"\n":
        return None, offset
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
        envelope = Envelope.from_json_dict(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError):
        return None, offset
    return envelope, frame_end


def iter_frames_with_offsets(buf, start_offset: int, limit: int):
    """Yield (envelope, offset_after_frame) starting at `start_offset`, stopping cleanly
    at the first frame that fails to decode (or at `limit`)."""
    offset = start_offset
    while True:
        envelope, next_offset = decode_frame_at(buf, offset, limit)
        if envelope is None:
            return
        yield envelope, next_offset
        offset = next_offset


def scan_ahead(buf, committed_offset: int, capacity: int, last_seq: int) -> tuple[int, int, int]:
    """Parse frames written past `committed_offset` up to `capacity`; returns
    (new_offset, new_last_seq, recovered_frame_count). Process-crash recovery (not
    power-loss-safe): trusts whole frames present past the last fsynced commit.
    """
    start = HEADER_SIZE + committed_offset
    limit = HEADER_SIZE + capacity
    offset = committed_offset
    seq = last_seq
    count = 0
    for envelope, next_offset in iter_frames_with_offsets(buf, start, limit):
        offset = next_offset - HEADER_SIZE
        seq = envelope.seq
        count += 1
    return offset, seq, count
