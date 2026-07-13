"""Seed the desk/book hierarchy and its risk limits from a data file - domain data
(set by risk managers), not deployment config. Initial limits are just the starting
state; runtime changes flow as LIMIT_CHANGE events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from hobo.risk.model import Limit


@dataclass(frozen=True)
class BookSeed:
    book_id: str
    limits: dict[Limit, float]  # NOTIONAL + DRAWDOWN + POSITION


@dataclass(frozen=True)
class DeskSeed:
    desk_id: str
    limits: dict[Limit, float]  # NOTIONAL + DRAWDOWN
    books: list[BookSeed]


class SeedError(ValueError):
    pass


def load_desk_seed(path: str) -> DeskSeed:
    try:
        with open(path) as f:
            raw = json.load(f)
        desk = DeskSeed(
            desk_id=raw["desk_id"],
            limits={
                Limit.NOTIONAL: raw["desk"]["notional_limit_usdt"],
                Limit.DRAWDOWN: raw["desk"]["drawdown_limit_usdt"],
            },
            books=[
                BookSeed(
                    book_id=b["book_id"],
                    limits={
                        Limit.NOTIONAL: b["notional_limit_usdt"],
                        Limit.DRAWDOWN: b["drawdown_limit_usdt"],
                        Limit.POSITION: b["position_limit"],
                    },
                )
                for b in raw["books"]
            ],
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SeedError(f"invalid desk seed at {path!r}: {exc}") from exc

    if not desk.books:
        raise SeedError(f"desk seed at {path!r} has no books")
    return desk
