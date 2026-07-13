from __future__ import annotations

from hobo.core.health import FEED_STALE_REASON, FeedHealthMonitor
from hobo.core.state_store import StateStore
from hobo.log.writer import LogWriter
from hobo.risk.staleness import StalenessWatchdog

from conftest import INSTRUMENT_ID, make_state


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def store_for(tmp_path, instrument, *, kill_switch=False, kill_switch_reason=""):
    state = make_state(instrument)
    state.desk.kill_switch = kill_switch
    state.desk.kill_switch_reason = kill_switch_reason
    writer = LogWriter(str(tmp_path / "eventlog.bin"))
    return StateStore(state, writer), writer


def mark_event():
    from hobo.core.events import MarkEvent

    return MarkEvent(INSTRUMENT_ID, 50_000, 1)


def test_check_trips_desk_kill_switch_when_feed_goes_stale(tmp_path, instrument):
    store, writer = store_for(tmp_path, instrument)
    clock = FakeClock()
    monitor = FeedHealthMonitor(store, StalenessWatchdog(threshold_s=5.0, clock=clock), store.state.desk.desk_id)

    monitor.on_message(mark_event())  # a tick arrives, feed is fresh
    assert store.state.desk.kill_switch is False

    clock.advance(6.0)  # no ticks for longer than the threshold
    monitor.check()
    assert store.state.desk.kill_switch is True
    assert store.state.desk.kill_switch_reason == FEED_STALE_REASON

    monitor.on_message(mark_event())  # ticks resume -> clears
    assert store.state.desk.kill_switch is False
    assert store.state.desk.kill_switch_reason == ""

    writer.close()


def test_persisted_feed_stale_kill_switch_clears_on_first_tick_after_restart(tmp_path, instrument):
    # Desk was recovered from a snapshot in the feed-stale killed state; the watchdog
    # restarts fresh (in-memory). The monitor must seed the watchdog stale so the first
    # resumed tick clears the kill switch.
    store, writer = store_for(tmp_path, instrument, kill_switch=True, kill_switch_reason=FEED_STALE_REASON)
    watchdog = StalenessWatchdog(threshold_s=5.0, clock=FakeClock())

    monitor = FeedHealthMonitor(store, watchdog, store.state.desk.desk_id)
    assert watchdog.stale is True  # seeded stale to match the persisted kill switch

    monitor.on_message(mark_event())  # first resumed tick
    assert store.state.desk.kill_switch is False
    assert store.state.desk.kill_switch_reason == ""

    writer.close()


def test_kill_switch_for_other_reason_is_not_auto_cleared(tmp_path, instrument):
    # A manual (non-feed) kill switch must survive restart - the monitor only owns
    # the feed-stale one, so it neither seeds the watchdog stale nor clears this.
    store, writer = store_for(tmp_path, instrument, kill_switch=True, kill_switch_reason="manual halt")
    watchdog = StalenessWatchdog(threshold_s=5.0, clock=FakeClock())

    monitor = FeedHealthMonitor(store, watchdog, store.state.desk.desk_id)
    assert watchdog.stale is False

    monitor.on_message(mark_event())
    assert store.state.desk.kill_switch is True
    assert store.state.desk.kill_switch_reason == "manual halt"

    writer.close()
