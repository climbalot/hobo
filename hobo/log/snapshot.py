"""Periodic durable snapshots of the folded risk state, bounding replay time and
disk growth (recovery replays only events since the last snapshot). Written
atomically (write .tmp, fsync, os.replace) so a crash mid-write can't leave a
half-written snapshot; JSON, not pickle, for introspectability.
"""

from __future__ import annotations

import json
import os
import re

from hobo.risk.state import State

SCHEMA_VERSION = 1
DEFAULT_KEEP_LAST = 3

_SNAPSHOT_RE = re.compile(r"^snapshot-(\d+)\.json$")


def snapshot_filename(seq: int) -> str:
    return f"snapshot-{seq}.json"


def write_snapshot(directory: str, state: State, keep_last: int = DEFAULT_KEEP_LAST) -> str:
    os.makedirs(directory, exist_ok=True)
    seq = state.last_seq
    final_path = os.path.join(directory, snapshot_filename(seq))
    tmp_path = final_path + ".tmp"

    payload = {"schema_version": SCHEMA_VERSION, "seq": seq, "state": state.to_dict()}
    with open(tmp_path, "w") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, final_path)

    _prune_old_snapshots(directory, keep_last)
    return final_path


def _list_snapshots(directory: str) -> list[tuple[int, str]]:
    if not os.path.isdir(directory):
        return []
    found = []
    for name in os.listdir(directory):
        m = _SNAPSHOT_RE.match(name)
        if m:
            found.append((int(m.group(1)), os.path.join(directory, name)))
    return sorted(found)


def _prune_old_snapshots(directory: str, keep_last: int) -> None:
    if keep_last <= 0:
        return
    for _, path in _list_snapshots(directory)[:-keep_last]:
        os.remove(path)


def load_latest_snapshot_payload(directory: str) -> dict | None:
    snapshots = _list_snapshots(directory)
    if not snapshots:
        return None
    _, path = snapshots[-1]
    with open(path) as f:
        return json.load(f)


def load_latest_state(directory: str) -> State | None:
    payload = load_latest_snapshot_payload(directory)
    if payload is None:
        return None
    return State.from_dict(payload["state"])
