"""Yahoo polling lifecycle reuses the original Follower and shared environment."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from .clock import safe_cutoff, utc
from .data import DataError
from .follow import Follower
from .replay import groups
from .yahoo import YahooCalendar, INTERVALS


class YahooFollower(Follower):
    def __init__(self, environment, provider, start=None):
        self.env, self.provider = environment, provider
        self.config = replace(environment.config, poll_interval_seconds=environment.config.yahoo_poll_interval_seconds)
        if not INTERVALS[self.config.yahoo_interval]:
            raise DataError("Yahoo follow requires an intraday interval (default 5m); use download/replay for daily/weekly/monthly")
        cutoff = safe_cutoff(environment.clock, self.config)
        reference = YahooCalendar({"calendar": "NASDAQ", "timezone": "America/New_York"})
        expected = reference.expected_end(cutoff, INTERVALS[self.config.yahoo_interval])
        if start is None:
            start = next(opening for _, opening, closing in reference.around(expected) if opening < expected <= closing)
        super().__init__(environment, provider, start=start)
        self.config = replace(environment.config, poll_interval_seconds=environment.config.yahoo_poll_interval_seconds)
        self.known_splits = set()
        self.provider.source_dir = Path(self.config.data_dir) / "yahoo" / "otc" / "intraday" / "sources"

    def poll_once(self):
        if self.env.ended or self.env.closed or self.stop_event.is_set():
            return
        cutoff = safe_cutoff(self.env.clock, self.config)
        minutes = INTERVALS[self.config.yahoo_interval]
        all_bars, successful, splits = [], [], set()
        for symbol in self.config.symbols:
            if self.stop_event.is_set():
                return
            # Incremental cursors commit only successful responses, retaining overlap
            # for revisions. No missing-trade grid is fabricated for illiquid OTC.
            start = max(self.start, self.through[symbol] - timedelta(minutes=max(minutes * 2, self.config.overlap_minutes)))
            if cutoff - start < timedelta(minutes=minutes):
                continue
            try:
                bars, end = self.provider.fetch((symbol,), start, cutoff)
            except DataError as exc:
                self.provider.reports[symbol] = {"status": "request_failed", "error": str(exc)}
                bars = []
            if self.stop_event.is_set() or self.env.closed:
                return
            report = dict(self.provider.reports.get(symbol, {}))
            security = self.provider.securities.get(symbol, {})
            if report.get("status") == "ok" and security.get("currency") != "USD":
                report = {"status": "unsupported_currency", "error": "Local USD account has no FX conversion; data remains downloadable"}
                bars = []
            with self.env.lock:
                self.env.data_status[symbol] = report
                self.env.acquisition.append({"wall_time": self.env.clock.now(), "provider": "Yahoo", "symbol": symbol,
                                             "start": str(start), "requested_end": str(cutoff), **report})
            if report.get("status") == "ok":
                successful.append(symbol)
                self.through[symbol] = end
                all_bars.extend(bars)
                for action in self.provider.actions.get(symbol, []):
                    if action["kind"] == "split":
                        splits.add((symbol, action["timestamp"], action["value"]))
        with self.env.lock:
            if self.env.market_time and splits - self.known_splits:
                self.env.connection = "stopped: newly reported split changes Yahoo's share basis; start a new account"
                self.env.finish("Yahoo split boundary; no holdings carried through a change of share basis")
                return
            self.known_splits.update(splits)
            self.env.dataset["securities"] = dict(self.provider.securities)
            self.env.dataset["corporate_actions"] = dict(self.provider.actions)
            known = [self.provider.securities[s] for s in successful]
            if known:
                signatures = {(s.get("calendar"), s.get("timezone")) for s in self.provider.securities.values()}
                if len(signatures) > 1:
                    self.env.connection = "paused: Yahoo follow supports one calendar/timezone per account"
                    return
                self.env.calendar = YahooCalendar(known[0])
            self.env.connection = "connected" if successful else "paused: Yahoo request failed, no data, or backoff active; inspect status"
            for batch in groups(all_bars, self.config.symbols):
                self.env.ingest(batch)
            # Unlike Alpaca's single-session accounts, Yahoo polling stays alive
            # through session breaks until manually stopped (or split-basis stop).
