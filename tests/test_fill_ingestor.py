from __future__ import annotations

from hobo.core.events import FillEvent
from hobo.core.fill_ingestor import FillIngestor
from hobo.log.events import EventType
from hobo.log.writer import LogWriter

from conftest import INSTRUMENT_ID, fill


def make_fill(trade_id: str = "", qty: float = 1.0, book_id: str = "A") -> FillEvent:
    return FillEvent(
        order_id="o1",
        book_id=book_id,
        instrument_id=INSTRUMENT_ID,
        side="BUY",
        qty=qty,
        fill_price=50_000,
        trade_id=trade_id,
    )


def test_submit_publishes_first_time_and_dedups_by_trade_id():
    published: list[FillEvent] = []
    ingestor = FillIngestor(published.append)

    assert ingestor.submit(make_fill(trade_id="T1")) is True
    assert len(published) == 1

    # Same trade_id from a second source: dropped, not published.
    assert ingestor.submit(make_fill(trade_id="T1")) is False
    assert len(published) == 1


def test_submit_admits_distinct_trade_ids():
    published: list[FillEvent] = []
    ingestor = FillIngestor(published.append)
    assert ingestor.submit(make_fill(trade_id="T1")) is True
    assert ingestor.submit(make_fill(trade_id="T2")) is True
    assert len(published) == 2


def test_trade_id_less_fill_is_always_admitted():
    published: list[FillEvent] = []
    ingestor = FillIngestor(published.append)
    # Paper fills carry no trade_id - each is admitted, never deduped.
    assert ingestor.submit(make_fill(trade_id="")) is True
    assert ingestor.submit(make_fill(trade_id="")) is True
    assert len(published) == 2


def test_seed_from_log_rejects_already_logged_trade_id(tmp_path):
    log_path = str(tmp_path / "eventlog.bin")
    writer = LogWriter(log_path)
    writer.append(EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 1, 50_000, trade_id="T1"), ts_ns=10)
    writer.append(EventType.FILL, fill("o2", "A", INSTRUMENT_ID, "BUY", 1, 50_000, trade_id="T2"), ts_ns=20)
    writer.close()

    published: list[FillEvent] = []
    ingestor = FillIngestor(published.append)
    ingestor.seed_from_log(log_path)

    # A replayed trade_id is not re-admitted; a genuinely new one is.
    assert ingestor.submit(make_fill(trade_id="T1")) is False
    assert ingestor.submit(make_fill(trade_id="T3")) is True
    assert published == [make_fill(trade_id="T3")]


def test_last_fill_ns_is_newest_trade_id_bearing_fill(tmp_path):
    log_path = str(tmp_path / "eventlog.bin")
    writer = LogWriter(log_path)
    writer.append(EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 1, 50_000, trade_id="T1"), ts_ns=10)
    writer.append(EventType.FILL, fill("o2", "A", INSTRUMENT_ID, "BUY", 1, 50_000, trade_id="T2"), ts_ns=30)
    writer.close()

    ingestor = FillIngestor(lambda _f: None)
    ingestor.seed_from_log(log_path)
    assert ingestor.last_fill_ns() == 30


def test_last_fill_ns_stays_zero_with_only_synthetic_fills(tmp_path):
    log_path = str(tmp_path / "eventlog.bin")
    writer = LogWriter(log_path)
    # Synthetic net-patch fills carry no trade_id: the reconciler must stay forward-only.
    writer.append(EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 1, 50_000, trade_id=""), ts_ns=10)
    writer.append(EventType.FILL, fill("o2", "A", INSTRUMENT_ID, "BUY", 1, 50_000, trade_id=""), ts_ns=20)
    writer.close()

    ingestor = FillIngestor(lambda _f: None)
    ingestor.seed_from_log(log_path)
    assert ingestor.last_fill_ns() == 0
