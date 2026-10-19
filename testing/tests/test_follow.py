from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from market_env.clock import FrozenClock, utc, NYSE
from market_env.config import Config
from market_env.data import Bar, DataError, FatalDataError
from market_env.environment import MarketEnvironment
from market_env.follow import Follower
from market_env.replay import synthetic_bars
import pytest
from threading import Event


def setup(clock_value="2026-09-01T13:47:30Z", bars=None):
    clock = FrozenClock(utc(clock_value))  # Eligible through 13:32.
    config = Config()
    env = MarketEnvironment([], config, clock, {"provider": "Alpaca", "feed": "sip"}, follow=True)
    class Provider:
        def __init__(self):
            self.calls, self.failed_symbol, self.failure = [], None, None
            self.bars = synthetic_bars() if bars is None else bars
        def fetch(self, symbols, start, end):
            self.calls.append((symbols, start, end))
            if self.failed_symbol in symbols:
                raise self.failure or DataError("outage")
            return [b for b in self.bars if b.symbol in symbols and b.start >= start and b.end <= end], end
    provider = Provider()
    follower = Follower(env, provider, start=utc("2026-09-01T13:30Z"))
    return env, provider, follower, clock


def test_calendar_holiday_early_close_weekend_and_dst():
    calendar = NYSE()
    assert calendar.sessions("2026-07-03", "2026-07-05") == ()
    early = calendar.sessions("2026-11-27", "2026-11-27")[0]
    assert early[2] == utc("2026-11-27T18:00Z")
    assert calendar.sessions("2026-03-06", "2026-03-06")[0][1].hour == 14
    assert calendar.sessions("2026-03-09", "2026-03-09")[0][1].hour == 13
    assert calendar.expected_end(utc("2026-09-05T18:00Z"), 1) == utc("2026-09-04T20:00Z")


def test_intentional_delay_not_stale_but_extra_lag_rejects_orders():
    env, provider, follower, clock = setup()
    follower.poll_once()
    diag = env.diagnostics()
    assert diag["market_time"] == utc("2026-09-01T13:32Z")
    assert diag["symbols"]["KO"]["data_age_seconds"] == 930
    assert diag["symbols"]["KO"]["unexpected_lag_seconds"] == 0
    assert not diag["symbols"]["KO"]["stale"]
    assert env.submit_order("KO", "buy", 1)["status"] == "pending"
    clock.value += timedelta(minutes=5)
    assert env.diagnostics()["symbols"]["KO"]["stale"]
    assert env.submit_order("KO", "buy", 1)["status"] == "rejected"


def test_overlap_dedup_reconnect_backfill_and_no_duplicate_fills():
    env, provider, follower, clock = setup()
    follower.poll_once()
    env.submit_order("KO", "buy", 2)
    follower.poll_once()
    assert len(env.price_history["KO"]) == 2 and not env.sim.fills
    clock.value += timedelta(minutes=3)
    provider.failed_symbol = "JPM"
    follower.poll_once()
    assert env.connection.startswith("paused") and env.market_time == utc("2026-09-01T13:32Z")
    assert follower.through["KO"] > follower.through["JPM"]
    assert env.submit_order("IBM", "buy", 1)["status"] == "rejected"
    provider.failed_symbol = None
    follower.poll_once()
    assert env.connection == "connected" and len(env.sim.fills) == 1
    assert env.sim.fills[0]["market_time"] == utc("2026-09-01T13:32Z")
    assert len(env.price_history["KO"]) == 5
    follower.poll_once()
    assert len(env.sim.fills) == 1 and len(env.price_history["KO"]) == 5


def test_revisions_ignored_late_bars_never_retrofill():
    env, provider, follower, clock = setup()
    follower.poll_once()
    original = env.latest["KO"]
    provider.bars = [replace(b, high=b.high + 50, close=b.close + 10) if b.symbol == "KO" else b for b in provider.bars]
    follower.poll_once()
    assert env.latest["KO"] == original and env.revisions
    assert all(r["kind"] == "vendor revision ignored" for r in env.revisions)
    old = replace(synthetic_bars()[0], symbol="IBM")
    env._seen.pop(("IBM", old.start))
    env.submit_order("IBM", "buy", 1)
    env.ingest([old])
    assert not env.sim.fills and env.revisions[-1]["kind"] == "late bar ignored for simulation"


def test_missing_symbol_waits_for_grace_and_repaired_before_exposure():
    original = synthetic_bars()
    env, provider, follower, clock = setup(bars=[b for b in original if not (b.symbol == "JPM" and b.start.minute == 31)])
    follower.poll_once()
    assert env.market_time == utc("2026-09-01T13:31Z")
    assert follower.missing["JPM"]
    provider.bars = original
    follower.poll_once()
    assert env.market_time == utc("2026-09-01T13:32Z") and not follower.missing["JPM"]


def test_old_gap_backfill_after_grace_does_not_change_past_observation():
    original = synthetic_bars()
    env, provider, follower, clock = setup("2026-09-01T13:52:30Z", bars=[b for b in original if not (b.symbol == "JPM" and b.start.minute == 30)])
    follower.poll_once()
    before = env.get_observation()["history"]
    provider.bars = original
    clock.value += timedelta(minutes=1)
    follower.poll_once()
    assert any(call[0] == ("JPM",) and call[1] == original[0].start for call in provider.calls[3:])
    assert not follower.missing["JPM"]
    assert env.get_observation()["history"]["JPM"][:len(before["JPM"])] == before["JPM"]
    assert any(r["kind"] == "late bar ignored for simulation" for r in env.revisions)


def test_fatal_entitlement_stops_cleanly():
    env, provider, follower, clock = setup()
    provider.failed_symbol, provider.failure = "KO", FatalDataError("Alpaca HTTP 403: entitlement")
    with pytest.raises(FatalDataError):
        follower.poll_once()
    assert env.ended and follower.stop_event.is_set() and "entitlement" in env.connection


def test_delayed_closing_bars_after_actual_close():
    bars = []
    for at in ("2026-09-01T19:58Z", "2026-09-01T19:59Z"):
        for symbol in Config().symbols:
            start = utc(at)
            bars.append(Bar(symbol, start, start + timedelta(minutes=1),
                            Decimal(60), Decimal(61), Decimal(59), Decimal(60), Decimal(100)))
    env, provider, _, clock = setup("2026-09-01T20:14:30Z", bars)
    follower = Follower(env, provider, start=bars[0].start)
    follower.poll_once()  # Market clock is still 19:59, despite wall market close.
    assert not env.diagnostics()["wall_session_open"] and env.diagnostics()["market_session_open"]
    order = env.submit_order("KO", "buy", 1)
    assert order["status"] == "pending"
    clock.value = utc("2026-09-01T20:15:30Z")
    follower.poll_once()
    assert env.market_time == utc("2026-09-01T20:00Z") and len(env.sim.fills) == 1
    assert env.sim.fills[0]["market_time"] == utc("2026-09-01T19:59Z")
    assert env.ended


def test_follow_rejects_ineligible_ingest_and_next():
    env, _, _, _ = setup()
    with pytest.raises(DataError, match="ineligible"):
        env.ingest(synthetic_bars())
    with pytest.raises(RuntimeError, match="replay only"):
        env.advance_replay()


def test_overnight_account_boundary_cancels_pending():
    env, _, _, clock = setup()
    env.ingest(synthetic_bars()[:3])
    env.connection = "connected"
    env.submit_order("KO", "buy", 1)
    next_day = [replace(b, start=b.start + timedelta(days=1), end=b.end + timedelta(days=1)) for b in synthetic_bars()[:3]]
    clock.value = utc("2026-09-02T15:00Z")
    env.ingest(next_day)
    assert env.ended and not env.sim.fills
    assert env.get_orders()[0]["status"] == "canceled"


def test_background_polling_without_console_decisions():
    env, provider, follower, _ = setup()
    finished = Event()
    original = follower.poll_once
    def poll():
        original()
        finished.set()
    follower.poll_once = poll
    follower.start_background()
    try:
        assert finished.wait(timeout=2)
        assert env.market_time == utc("2026-09-01T13:32Z")
        assert env.get_orders() == [] and env.sim.fills == []
    finally:
        follower.close()
    assert not follower.thread.is_alive()


def test_premarket_and_weekend_start_wait_for_actual_next_session():
    env, provider, _, _ = setup("2026-09-05T15:00Z")  # Saturday; Monday is Labor Day.
    follower = Follower(env, provider)
    assert follower.start == utc("2026-09-08T13:30Z")
    follower.poll_once()
    assert provider.calls == [] and not env.ended
    env, provider, _, _ = setup("2026-09-01T12:00Z")
    follower = Follower(env, provider)
    assert follower.start == utc("2026-09-01T13:30Z")
