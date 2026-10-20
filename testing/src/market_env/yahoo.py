"""Yahoo/yfinance adapter to the existing Bar, storage, and simulator interfaces.

No quote endpoint, bid/ask invention, or order transport is present here.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from threading import Event
from urllib.parse import quote
from zoneinfo import ZoneInfo
import calendar as month_calendar
import csv
import hashlib
import json
import time
import uuid

import pandas as pd
import pandas_market_calendars as mcal
import yfinance as yf
from yfinance.exceptions import YFRateLimitError

from .clock import utc, WallClock, NYSE, safe_cutoff
from .data import Bar, Budget, DataError, write_dataset

ADJUSTMENT = "yahoo_split_normalized_no_dividend_adjustment"
INTERVALS = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "90m": 90, "1h": 60,
             "1d": None, "1wk": None, "1mo": None}
OTC_EXCHANGES = {"PNK", "OQB", "OQX", "OTC", "OTCBB", "OBB"}
CALENDARS = {"NYQ": "NYSE", "NYSE": "NYSE", "NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ",
             "ASE": "NYSE", "PCX": "NYSE", "LSE": "LSE", "TOR": "TSX", "HKG": "HKEX"}


def yahoo_time(value) -> datetime:
    """Yahoo CLI date-only inputs mean midnight UTC; timestamps require offsets."""
    if isinstance(value, str) and len(value) == 10:
        value += "T00:00:00+00:00"
    return utc(value)


def security_metadata(raw):
    exchange = str(raw.get("exchangeName") or raw.get("exchange") or "")
    full = str(raw.get("fullExchangeName") or "")
    otc = True if exchange in OTC_EXCHANGES or "OTC" in full.upper() else (False if exchange in CALENDARS else None)
    reference = "NASDAQ" if otc else CALENDARS.get(exchange)
    return {"exchange": exchange or None, "exchange_name": full or None, "is_otc": otc,
            "currency": raw.get("currency"), "timezone": raw.get("exchangeTimezoneName"),
            "calendar": reference, "calendar_basis": "US equity reference, NOT an OTC venue schedule" if otc else
            ("exchange calendar" if reference else "unknown; conservative civil-period bounds"),
            "first_trade_timestamp": str(raw["firstTradeDate"]) if raw.get("firstTradeDate") is not None else None,
            "provider_delay_minutes": 15 if otc else None}


class YahooCalendar(NYSE):
    """Exchange calendar where known. OTC reference is explicit, not venue truth.

    Observed bars are authoritative about activity: missing regular-session bars
    never prove a halt, closure, or inactivity. Unknown venues use civil periods.
    """
    def __init__(self, security=None):
        security = security or {}
        self.name = security.get("calendar")
        self.is_otc = security.get("is_otc") is True
        self.timezone = ZoneInfo(security.get("timezone") or "UTC")
        self.calendar = mcal.get_calendar(self.name) if self.name else None

    @lru_cache(maxsize=1024)
    def sessions(self, start, end):
        if self.calendar is not None:
            return super().sessions(start, end)
        days = pd.date_range(start, end, freq="D")
        return tuple((str(day.date()), datetime.combine(day.date(), datetime.min.time(), self.timezone).astimezone(timezone.utc),
                      datetime.combine((day + pd.Timedelta(days=1)).date(), datetime.min.time(), self.timezone).astimezone(timezone.utc)) for day in days)

    def period_bounds(self, timestamp, interval):
        local = timestamp.astimezone(self.timezone)
        if INTERVALS[interval] is not None:
            start = utc(timestamp)
            end = start + timedelta(minutes=INTERVALS[interval])
            # Last intraday interval may end at an early/regular reference close.
            for _, opening, closing in self.sessions(local.date().isoformat(), local.date().isoformat()):
                if opening <= start < closing:
                    end = min(end, closing)
            return start, end
        first = local.date()
        if interval == "1wk":
            first -= timedelta(days=first.weekday())
            last = first + timedelta(days=6)
        elif interval == "1mo":
            first = first.replace(day=1)
            last = first.replace(day=month_calendar.monthrange(first.year, first.month)[1])
        else:
            last = first
        sessions = self.sessions(first.isoformat(), last.isoformat())
        if not sessions:
            if self.is_otc:
                # A supplied OTC bar overrides a reference-calendar closure.
                # Use conservative civil bounds rather than deleting that activity.
                begin = datetime.combine(first, datetime.min.time(), self.timezone)
                finish = datetime.combine(last + timedelta(days=1), datetime.min.time(), self.timezone)
                return begin.astimezone(timezone.utc), finish.astimezone(timezone.utc)
            raise DataError(f"No reference session for period {first}")
        return sessions[0][1], sessions[-1][2]


def dataset_calendar(meta):
    security = meta.get("securities", {}).get(meta.get("symbols", [None])[0], {})
    return YahooCalendar(security)


def validate_yahoo_bars(bars, symbols, interval, securities=None):
    if interval not in INTERVALS or not symbols or len(set(symbols)) != len(symbols):
        raise DataError("Invalid Yahoo interval/symbol metadata")
    seen = set()
    calendars = {symbol: YahooCalendar(securities.get(symbol, {})) for symbol in symbols} if securities is not None else {}
    for bar in bars:
        if bar.symbol not in symbols or (bar.symbol, bar.start) in seen:
            raise DataError("Unexpected symbol or duplicate Yahoo bar")
        seen.add((bar.symbol, bar.start))
        if bar.start.utcoffset() != timedelta(0) or bar.end.utcoffset() != timedelta(0) or bar.start >= bar.end:
            raise DataError("Yahoo bars require increasing aware UTC bounds")
        duration = INTERVALS[interval]
        if duration and (bar.end - bar.start > timedelta(minutes=duration) or bar.start.second or bar.start.microsecond):
            raise DataError("Invalid Yahoo intraday bounds")
        if securities is not None:
            expected = calendars[bar.symbol].period_bounds(bar.start, interval)
            if expected != (bar.start, bar.end):
                raise DataError("Yahoo bar bounds do not match the declared complete interval")
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not p.is_finite() or p <= 0 for p in prices):
            raise DataError("Yahoo prices must be finite and positive")
        if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise DataError("Invalid Yahoo OHLC")
        # NaN volume is retained and masked/warned; never changed to invented volume.
        if not bar.volume.is_nan() and (not bar.volume.is_finite() or bar.volume < 0):
            raise DataError("Invalid Yahoo volume")


def normalize_history(symbol, frame, interval, security, start, end):
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise DataError("No data: ticker unavailable/delisted, no trades, or interval/range unsupported; try --interval 1d")
    required = {"Open", "High", "Low", "Close", "Volume"}
    if not required <= set(frame.columns) or isinstance(frame.columns, pd.MultiIndex):
        raise DataError("Yahoo response is missing required OHLCV columns")
    cal = YahooCalendar(security)
    bars, actions, diagnostics, unique = [], [], [], {}
    for index, row in frame.sort_index(kind="stable").iterrows():
        try:
            timestamp = pd.Timestamp(index)
            if timestamp.tzinfo is None:
                if not security.get("timezone"):
                    raise DataError("Naive Yahoo timestamp without exchange timezone metadata")
                timestamp = timestamp.tz_localize(security["timezone"], ambiguous="raise", nonexistent="raise")
            at = utc(timestamp.to_pydatetime())
            opening, closing = cal.period_bounds(at, interval)
            for column, kind in (("Dividends", "dividend"), ("Stock Splits", "split")):
                value = row.get(column)
                if value is not None and pd.notna(value) and Decimal(str(value)) != 0 and start <= at <= end:
                    actions.append({"symbol": symbol, "timestamp": at.isoformat(), "kind": kind, "value": str(value),
                                    "timestamp_basis": "provider period label; weekly/monthly actions may be aggregated"})
            if opening < start or closing > end:
                continue  # Hide partial aggregate periods, including current day/week/month.
            volume = Decimal("NaN") if pd.isna(row["Volume"]) else Decimal(str(row["Volume"]))
            bar = Bar(symbol, opening, closing, *(Decimal(str(row[c])) for c in ("Open", "High", "Low", "Close")), volume)
            validate_yahoo_bars([bar], [symbol], interval)
            if opening in unique:
                diagnostics.append(f"duplicate {opening.isoformat()} ignored (first valid row retained)")
                continue
            unique[opening] = bar
            bars.append(bar)
        except (ValueError, TypeError, ArithmeticError, DataError) as exc:
            diagnostics.append(f"invalid row {index}: {exc}")
    if not bars:
        raise DataError("No valid completed Yahoo bars in the requested range; try daily data or an older end date. " + "; ".join(diagnostics[:3]))
    return bars, actions, diagnostics


def yahoo_coverage(bars, symbols, start, end, interval, securities):
    result = {}
    for symbol in symbols:
        selected = sorted((b for b in bars if b.symbol == symbol), key=lambda b: b.start)
        low = sum(b.volume.is_nan() or b.volume <= 0 for b in selected)
        cal = YahooCalendar(securities.get(symbol, {}))
        observed_days = {b.start.astimezone(cal.timezone).date().isoformat() for b in selected}
        sessions = cal.sessions(start.astimezone(cal.timezone).date().isoformat(), end.astimezone(cal.timezone).date().isoformat())
        # Reference-session gaps are diagnostics, not proof of expected OTC trades.
        reference_gaps = [day for day, opening, closing in sessions if opening >= start and closing <= end and day not in observed_days]
        result[symbol] = {"bars": len(selected), "first": selected[0].start.isoformat() if selected else None,
                          "last": selected[-1].end.isoformat() if selected else None,
                          "zero_or_missing_volume_bars": low, "reference_session_gaps": reference_gaps[:30] if interval == "1d" else [],
                          "gap_interpretation": "No fabricated bars. Missing bars may mean no trades or missing vendor coverage.",
                          "interval": interval}
    return result


class YahooProvider:
    """Same fetch(symbols, start, end)->(bars,effective_end) boundary as Alpaca."""
    name = "yahoo"

    def __init__(self, config, root: Path, clock=None, ticker_factory=None, sleep=None, budget=None):
        self.config, self.root, self.clock = config, Path(root), clock or WallClock()
        self.stop_event = Event()
        self.factory = ticker_factory or yf.Ticker
        self.interval = config.yahoo_interval
        self.tickers, self.reports, self.securities, self.actions = {}, {}, {}, {}
        self.next_attempt = {}
        def pause(seconds):
            if sleep:
                sleep(seconds)
            else:
                self.stop_event.wait(seconds)
            if self.stop_event.is_set():
                raise DataError("Yahoo request canceled")
        self.sleep = pause
        self.budget = budget or Budget(config.yahoo_requests_per_minute, sleep=self.sleep)
        cache = self.root / ".cache" / "yfinance"
        cache.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(cache))
        self.source_dir = None

    def fetch(self, symbols, start, end):
        start, requested_end = yahoo_time(start), yahoo_time(end)
        end = min(requested_end, safe_cutoff(self.clock, self.config))
        if start >= end:
            raise DataError("Empty Yahoo range after complete-bar cutoff")
        if end < requested_end:
            print(f"Yahoo effective request: {start.isoformat()} through {end.isoformat()}")
        # Be explicit about availability; no interval fallback or requests for old intraday windows.
        days = 7 if self.interval == "1m" else (730 if self.interval in ("60m", "1h") else 60)
        if INTERVALS[self.interval] and start < utc(self.clock.now()) - timedelta(days=days):
            raise DataError(f"Yahoo {self.interval} lookback is limited (conservative {days}-day bound); requested {start.isoformat()}. Use --interval 1d for older history; actual availability may be shorter.")
        bars = []
        for symbol in symbols:
            if self.stop_event.is_set():
                raise DataError("Yahoo request canceled")
            if self.next_attempt.get(symbol, end) > self.clock.now():
                self.reports[symbol] = {"status": "backoff", "error": "Yahoo backoff active", "retry_at": str(self.next_attempt[symbol])}
                continue
            try:
                self.budget.acquire()
                if self.stop_event.is_set():
                    raise DataError("Yahoo request canceled")
                ticker = self.tickers.setdefault(symbol, self.factory(symbol)) if symbol not in self.tickers else self.tickers[symbol]
                frame = ticker.history(start=start, end=end, interval=self.interval, auto_adjust=False,
                                       back_adjust=False, actions=True, repair=False, keepna=True,
                                       prepost=False, rounding=False, timeout=self.config.timeout_seconds, raise_errors=True)
                # Select keys only: copying lazy tradingPeriods can trigger another HTTP call.
                raw = ticker.history_metadata or {}
                sec = security_metadata({key: raw.get(key) for key in
                                        ("exchangeName", "exchange", "fullExchangeName", "currency", "exchangeTimezoneName", "firstTradeDate")})
                if not sec["timezone"] and isinstance(frame.index, pd.DatetimeIndex) and frame.index.tz:
                    sec["timezone"] = str(frame.index.tz)
                self.securities[symbol] = sec
                sec["original_ticker"] = symbol
                source = None
                if self.source_dir is not None:
                    self.source_dir.mkdir(parents=True, exist_ok=True)
                    source = self.source_dir / (quote(symbol, safe="") + "-" + uuid.uuid4().hex + ".csv")
                    frame.to_csv(source, lineterminator="\n")  # Preserve received pre-transform columns/actions.
                normalized, actions, diagnostics = normalize_history(symbol, frame, self.interval, sec, start, end)
                self.actions[symbol] = actions
                bars.extend(normalized)
                self.reports[symbol] = {"status": "ok", "diagnostics": diagnostics,
                                        "effective_available_start": normalized[0].start.isoformat(),
                                        "effective_available_end": normalized[-1].end.isoformat(),
                                        "source_file": source.relative_to(self.root).as_posix() if source and source.is_relative_to(self.root) else (str(source) if source else None),
                                        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest() if source else None}
            except Exception as exc:
                rate_limited = isinstance(exc, YFRateLimitError) or "429" in str(exc)
                self.next_attempt[symbol] = self.clock.now() + timedelta(seconds=self.config.yahoo_backoff_seconds)
                self.reports[symbol] = {"status": "rate_limited" if rate_limited else "request_failed",
                                        "error": str(exc), "retry_at": str(self.next_attempt[symbol])}
                print(f"Yahoo {symbol}: {exc}")
        order = {s: i for i, s in enumerate(symbols)}
        bars.sort(key=lambda b: (b.end, order[b.symbol]))
        return bars, end

    def dataset_metadata(self, bars, symbols, start, requested_end, end):
        return {"version": 2, "provider": "Yahoo", "feed": "yahoo", "adjustment": ADJUSTMENT,
                "interval": self.interval, "interval_minutes": INTERVALS[self.interval], "symbols": list(symbols),
                "retrieved_at": utc(self.clock.now()).isoformat(), "requested_start": utc(start).isoformat(),
                "requested_end": utc(requested_end).isoformat(), "effective_start": utc(start).isoformat(),
                "effective_end": utc(end).isoformat(), "securities": {s: self.securities.get(s, {}) for s in symbols},
                "corporate_actions": {s: self.actions.get(s, []) for s in symbols},
                "diagnostics": {s: self.reports.get(s, {}) for s in symbols},
                "coverage": yahoo_coverage(bars, symbols, start, end, self.interval, self.securities),
                "accounting_basis": "fixed Yahoo split-normalized share units; dividends not credited; price-only P/L",
                "execution": "estimated bar fills, not verified executions; no bid/ask data"}

    def close(self):
        self.stop_event.set()


def load_yahoo_dataset(path, meta):
    required = {"version", "sha256", "symbols", "provider", "feed", "adjustment", "interval", "retrieved_at",
                "requested_start", "requested_end", "effective_start", "effective_end", "securities", "coverage", "corporate_actions"}
    if not required <= meta.keys() or meta["version"] != 2 or meta["feed"] != "yahoo" or meta["adjustment"] != ADJUSTMENT:
        raise DataError("Invalid Yahoo metadata/adjustment basis")
    if hashlib.sha256(path.read_bytes()).hexdigest() != meta["sha256"]:
        raise DataError("Dataset hash mismatch")
    if path.suffix == ".parquet":
        rows = pd.read_parquet(path).to_dict("records")
    else:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    bars = [Bar(r["symbol"], utc(r["start"]), utc(r["end"]),
                *(Decimal(r[c]) for c in ("open", "high", "low", "close", "volume"))) for r in rows]
    validate_yahoo_bars(bars, meta["symbols"], meta["interval"], meta["securities"])
    start, end = utc(meta["effective_start"]), utc(meta["effective_end"])
    if not utc(meta["requested_start"]) <= start < end <= utc(meta["requested_end"]):
        raise DataError("Invalid Yahoo effective range")
    utc(meta["retrieved_at"])
    if not bars or any(b.start < start or b.end > end for b in bars):
        raise DataError("Empty Yahoo dataset or bars outside effective range")
    if yahoo_coverage(bars, meta["symbols"], start, end, meta["interval"], meta["securities"]) != meta["coverage"]:
        raise DataError("Yahoo coverage metadata mismatch")
    order = {s: i for i, s in enumerate(meta["symbols"])}
    return sorted(bars, key=lambda b: (b.end, order[b.symbol])), meta


def download_yahoo(provider, config, start, end, output, refresh=False, format="both"):
    """Per-ticker cached files by default, retaining original source responses."""
    from .data import load_dataset
    start, end = yahoo_time(start), yahoo_time(end)
    effective = min(end, safe_cutoff(provider.clock, config))
    if start >= effective:
        raise DataError("Empty Yahoo download range")
    output = Path(output)
    single_file = output.suffix in (".csv", ".parquet")
    if single_file and len(config.symbols) != 1:
        raise DataError("Yahoo file output accepts one ticker; use a directory for multiple tickers")
    directory = output.parent if single_file else output
    provider.source_dir = directory / "sources"
    results = {}
    for symbol in config.symbols:
        paths = [output] if single_file else [directory / (quote(symbol, safe="") + suffix)
                                                 for suffix in ((".csv", ".parquet") if format == "both" else ("." + format,))]
        identity = {"provider": "Yahoo", "symbols": [symbol], "interval": config.yahoo_interval,
                    "requested_start": start.isoformat(), "requested_end": end.isoformat(),
                    "effective_end": effective.isoformat(), "adjustment": ADJUSTMENT}
        cached = False
        if all(p.exists() for p in paths) and not refresh:
            loaded = [load_dataset(path) for path in paths]
            if not all(all(meta.get(k) == v for k, v in identity.items()) for _, meta in loaded):
                raise DataError(f"Existing Yahoo output has another identity: {paths[0]}; use another folder or --refresh")
            results[symbol] = {"status": "cached", "paths": [str(p) for p in paths], "coverage": loaded[0][1]["coverage"]}
            cached = True
        elif any(p.exists() for p in paths) and not refresh:
            raise DataError(f"Partial existing output for {symbol}; use --refresh or another folder")
        if cached:
            continue
        bars, actual_end = provider.fetch((symbol,), start, end)
        if not bars:
            results[symbol] = provider.reports.get(symbol, {"status": "unavailable"})
            continue
        meta = provider.dataset_metadata(bars, (symbol,), start, end, actual_end)
        for path in paths:
            write_dataset(path, bars, meta)
        results[symbol] = {"status": "downloaded", "paths": [str(p) for p in paths], "coverage": meta["coverage"],
                           "securities": meta["securities"], "diagnostics": meta["diagnostics"]}
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ("download-report-" + uuid.uuid4().hex + ".json")).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results
