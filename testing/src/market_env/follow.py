"""Incremental delayed historical polling. Decisions are never made here."""
from datetime import timedelta
from zoneinfo import ZoneInfo
from threading import Event, Thread

from .clock import safe_cutoff
from .data import DataError, FatalDataError
from .replay import groups


class Follower:
    def __init__(self, environment, provider, start=None):
        self.env, self.provider = environment, provider
        self.config = environment.config
        cutoff = safe_cutoff(environment.clock, self.config)
        expected = environment.calendar.expected_end(cutoff, self.config.interval_minutes)
        if start is None:
            if expected is None:
                raise DataError("No eligible exchange session found")
            # Before today's first delayed bar, wait for today's session rather
            # than starting and immediately ending yesterday's account.
            local_day = cutoff.astimezone(ZoneInfo("America/New_York")).date().isoformat()
            today = environment.calendar.sessions(local_day, local_day)
            if today:
                start = today[0][1]
            else:
                # Weekend/holiday startup waits for the next actual exchange session.
                upcoming = environment.calendar.sessions(local_day, (cutoff + timedelta(days=10)).date().isoformat())
                start = upcoming[0][1]
        self.start = start
        self.through = dict.fromkeys(self.config.symbols, start)
        self.missing = {s: set() for s in self.config.symbols}
        self.buffer = {}
        self.gap_attempt = {}
        self.poll_number = 0
        self.stop_event = Event()
        self.thread = None

    def _expected_starts(self, start, end):
        delta = timedelta(minutes=self.config.interval_minutes)
        for _, opening, closing in self.env.calendar.sessions(start.date().isoformat(), end.date().isoformat()):
            at = opening
            while at + delta <= min(end, closing):
                if at >= start:
                    yield at
                at += delta

    def _record(self, symbol, start, end, bars):
        received = {b.start for b in bars}
        self.missing[symbol].update(set(self._expected_starts(start, end)) - received)
        self.missing[symbol].difference_update(received)
        with self.env.lock:
            self.env.acquisition.append({"wall_time": self.env.clock.now(), "symbol": symbol,
                                         "start": start, "end": end, "received": len(bars),
                                         "unresolved_gaps": len(self.missing[symbol])})
            for bar in bars:
                key = (bar.symbol, bar.start)
                if key in self.env._seen:
                    self.env._process([bar])  # Logs revisions; never revises a fill/mark.
                else:
                    self.buffer[key] = bar

    def poll_once(self):
        if self.env.ended or self.env.closed:
            return
        self.poll_number += 1
        cutoff = safe_cutoff(self.env.clock, self.config)
        delta = timedelta(minutes=self.config.interval_minutes)
        try:
            # Each symbol commits progress only after the SDK consumes every page.
            for symbol in self.config.symbols:
                overlap = max(self.config.overlap_minutes, self.config.interval_minutes)
                start = max(self.start, self.through[symbol] - timedelta(minutes=overlap))
                if cutoff - start < delta:
                    continue
                bars, end = self.provider.fetch((symbol,), start, cutoff)
                self._record(symbol, start, end, bars)
                self.through[symbol] = end
                # One bounded old-gap window per poll, separate from the incremental range.
                old = sorted((t for t in self.missing[symbol] if t < start),
                             key=lambda t: (self.gap_attempt.get((symbol, t), 0), t))
                if old:
                    gap_start = old[0]
                    gap_end = min(start, gap_start + timedelta(minutes=10))
                    if gap_end - gap_start >= delta:
                        repaired, effective = self.provider.fetch((symbol,), gap_start, gap_end)
                        self._record(symbol, gap_start, effective, repaired)
                        for at in self._expected_starts(gap_start, effective):
                            self.gap_attempt[symbol, at] = self.poll_number
            with self.env.lock:
                self.env.connection = "connected"
                for batch in groups(list(self.buffer.values()), self.config.symbols):
                    at = batch[0].end
                    complete = {b.symbol for b in batch} == set(self.config.symbols)
                    grace_expired = (cutoff - at).total_seconds() >= self.config.stale_tolerance_seconds
                    if not complete and not grace_expired:
                        break  # Preserve chronology while waiting for a late symbol.
                    self.env.ingest(batch)
                    for bar in batch:
                        self.buffer.pop((bar.symbol, bar.start), None)
                # End after delayed close even if the provider omitted the final bar.
                if self.env.session:
                    closing = next(c for day, _, c in self.env.calendar.around(cutoff) if day == self.env.session)
                    if cutoff >= closing + timedelta(seconds=self.config.stale_tolerance_seconds):
                        self.env.finish("delayed session complete; missing bars remain missing")
        except FatalDataError as exc:
            with self.env.lock:
                self.env.connection = f"stopped: {exc}"
                self.env.acquisition.append({"wall_time": self.env.clock.now(), "error": str(exc), "fatal": True})
                self.env.finish("authentication/entitlement failure")
            self.stop_event.set()
            raise
        except DataError as exc:
            with self.env.lock:
                self.env.connection = f"paused: {exc}"
                self.env.acquisition.append({"wall_time": self.env.clock.now(), "error": str(exc), "fatal": False})
            # Keep all cursors/buffer: next poll backfills the failed symbol's range.

    def _run(self):
        while not self.stop_event.is_set() and not self.env.ended:
            try:
                self.poll_once()
            except FatalDataError:
                print(f"\n{self.env.connection}")
                break
            except Exception as exc:
                with self.env.lock:
                    self.env.connection = f"stopped: unexpected {type(exc).__name__}"
                    self.env.finish("poller failure")
                break
            self.stop_event.wait(self.config.poll_interval_seconds)

    def start_background(self):
        self.thread = Thread(target=self._run, name="delayed-sip-poller", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if hasattr(self.provider, "stop_event"):
            self.provider.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2 * self.config.timeout_seconds + 2)
