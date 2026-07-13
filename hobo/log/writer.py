"""Single-writer append-only mmap log. `append()` writes into the mmap and returns
immediately (never blocks the hot path); `commit()` fsyncs the data region then
advances the header's `committed_offset` - data is durable before the pointer that
claims it, so a crash loses at most the uncommitted tail. On reopen, whole frames
past the last commit are recovered via scan_ahead and re-committed (safe: they
survive a process crash in the page cache; only power loss, which committed_offset
guards, would lose them).
"""

from __future__ import annotations

import mmap
import os

from hobo.log import format as fmt
from hobo.log.events import Envelope

DEFAULT_INITIAL_CAPACITY = 1 << 20  # 1 MiB
DEFAULT_GROWTH_FACTOR = 2


class LogWriter:
    def __init__(
        self,
        path: str,
        initial_capacity: int = DEFAULT_INITIAL_CAPACITY,
        growth_factor: int = DEFAULT_GROWTH_FACTOR,
    ) -> None:
        self.path = path
        self.growth_factor = growth_factor

        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)  # e.g. data/local/ on first run
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)

        if is_new:
            os.ftruncate(self._fd, fmt.HEADER_SIZE + initial_capacity)
            os.pwrite(self._fd, fmt.pack_header(initial_capacity, 0, 0), 0)
            os.fsync(self._fd)

        self._mm = mmap.mmap(self._fd, 0)
        header = fmt.unpack_header(self._mm)
        self.capacity = header.capacity
        self.committed_offset = header.committed_offset
        self.last_seq = header.last_seq

        recovered_offset, recovered_seq, recovered_count = fmt.scan_ahead(
            self._mm, self.committed_offset, self.capacity, self.last_seq
        )
        self.write_offset = recovered_offset
        self.last_seq = recovered_seq
        self.recovered_uncommitted_count = recovered_count
        if recovered_offset != self.committed_offset:
            self._do_commit()

    def append(self, event_type: str, payload: dict, ts_ns: int) -> Envelope:
        seq = self.last_seq + 1
        envelope = Envelope(seq=seq, ts_ns=ts_ns, event_type=event_type, payload=payload)
        frame = fmt.encode_frame(envelope)
        self._ensure_capacity(len(frame))

        start = fmt.HEADER_SIZE + self.write_offset
        self._mm[start : start + len(frame)] = frame
        self.write_offset += len(frame)
        self.last_seq = seq
        return envelope

    def _ensure_capacity(self, needed: int) -> None:
        while self.write_offset + needed > self.capacity:
            self._grow()

    def _grow(self) -> None:
        new_capacity = self.capacity * self.growth_factor
        self._mm.flush()
        self._mm.close()
        os.ftruncate(self._fd, fmt.HEADER_SIZE + new_capacity)
        self._mm = mmap.mmap(self._fd, 0)
        self.capacity = new_capacity
        fmt.write_capacity(self._mm, new_capacity)

    def commit(self) -> bool:
        """fsync-batch entry point. Returns True if there was anything new to commit."""
        if self.write_offset == self.committed_offset:
            return False
        self._do_commit()
        return True

    def _do_commit(self) -> None:
        self._mm.flush()
        os.fsync(self._fd)
        fmt.write_committed(self._mm, self.write_offset, self.last_seq)
        self._mm.flush()
        os.fsync(self._fd)
        self.committed_offset = self.write_offset

    def close(self) -> None:
        self.commit()
        self._mm.close()
        os.close(self._fd)

    def __enter__(self) -> "LogWriter":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
