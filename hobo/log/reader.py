"""Read-only access to the event log: power-loss-safe replay (up to
`committed_offset` only) for crash recovery and live tailing for the shadow replica.
"""

from __future__ import annotations

import mmap
import os

from hobo.log import format as fmt
from hobo.log.events import Envelope


def open_readonly(path: str) -> mmap.mmap:
    fd = os.open(path, os.O_RDONLY)
    try:
        return mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
    finally:
        os.close(fd)  # the mmap keeps the file mapped; the fd itself isn't needed after this


def read_header(mm) -> fmt.HeaderFields:
    return fmt.unpack_header(mm)


def replay(mm) -> list[Envelope]:
    """Parse frames from the start of the data region up to the header's
    `committed_offset` only - the durability-guaranteed replay used by crash recovery."""
    header = fmt.unpack_header(mm)
    limit = fmt.HEADER_SIZE + header.committed_offset
    return [envelope for envelope, _ in fmt.iter_frames_with_offsets(mm, fmt.HEADER_SIZE, limit)]


def replay_range(mm, start_offset: int, end_offset: int) -> list[Envelope]:
    """Parse frames in the data-region-relative range [start_offset, end_offset)."""
    start = fmt.HEADER_SIZE + start_offset
    limit = fmt.HEADER_SIZE + end_offset
    return [envelope for envelope, _ in fmt.iter_frames_with_offsets(mm, start, limit)]
