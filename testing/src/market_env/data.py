"""SIP-only downloads, immutable CSV identities, and strict bar validation."""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from collections import deque
import csv
import hashlib
import json
import os
import time
from threading import Event

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed, Adjustment
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from dotenv import dotenv_values
import requests

from .clock import utc, safe_cutoff, NYSE, WallClock
from .config import Config


class DataError(RuntimeError):
    """Provider/configuration error safe to display; never includes credentials."""


class FatalDataError(DataError):
    pass


@dataclass(frozen=True)
class Bar:
    symbol: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def row(self):
        return {key: str(value) for key, value in asdict(self).items()}


def validate_bars(bars: list[Bar], symbols, minutes: int, calendar=None):
    calendar = calendar or NYSE()
    keys = set()
    for bar in bars:
        if bar.symbol not in symbols:
            raise DataError(f"Unexpected symbol {bar.symbol}")
        if bar.start != utc(bar.start) or bar.start.utcoffset() != timedelta(0):
            raise DataError("Bar timestamps must be UTC")
        utc(bar.end)
        if bar.end.utcoffset() != timedelta(0) or bar.end - bar.start != timedelta(minutes=minutes):
            raise DataError("Invalid bar interval")
        if bar.start.second or bar.start.microsecond or bar.start.minute % minutes:
            raise DataError("Unaligned bar start")
        if not calendar.session_for(bar.start, bar.end):
            raise DataError("Only complete NYSE regular-session bars are supported")
        key = (bar.symbol, bar.start)
        if key in keys:
            raise DataError(f"Duplicate bar: {key}")
        keys.add(key)
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not x.is_finite() or x <= 0 for x in prices):
            raise DataError("Prices must be positive and finite")
        if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise DataError("Inconsistent OHLC")
        if not bar.volume.is_finite() or bar.volume < 0:
            raise DataError("Invalid volume")


def coverage(bars, symbols, start, end, minutes, calendar=None):
    calendar = calendar or NYSE()
    delta = timedelta(minutes=minutes)
    expected = set()
    for _, opening, closing in calendar.sessions(start.date().isoformat(), end.date().isoformat()):
        at = opening
        while at + delta <= min(end, closing):
            if at >= start:
                expected.add(at)
            at += delta
    result = {}
    for symbol in symbols:
        times = sorted(b.start for b in bars if b.symbol == symbol)
        missing = sorted(expected - set(times))
        result[symbol] = {"bars": len(times), "first": str(times[0]) if times else None,
                          "last": str(times[-1]) if times else None,
                          "missing_expected_bars": len(missing),
                          "gap_examples": [str(t) for t in missing[:20]]}
    return result


class Budget:
    """Sliding-window budget applies to EVERY HTTP attempt, including SDK pages."""
    def __init__(self, limit, monotonic=time.monotonic, sleep=time.sleep):
        self.limit, self.monotonic, self.sleep = limit, monotonic, sleep
        self.attempts = deque()

    def acquire(self):
        while True:
            now = self.monotonic()
            while self.attempts and now - self.attempts[0] >= 60:
                self.attempts.popleft()
            if len(self.attempts) < self.limit:
                self.attempts.append(now)
                return
            self.sleep(max(0.001, 60 - (now - self.attempts[0])))


def credentials(root: Path):
    # Read exactly this project's .env, never dotenv's parent-directory discovery.
    values = dotenv_values(root / ".env") if (root / ".env").exists() else {}
    key = os.environ.get("ALPACA_API_KEY") or values.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY") or values.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise FatalDataError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY. Set environment variables or this project's .env.")
    return key, secret


class AlpacaProvider:
    def __init__(self, config: Config, root: Path, clock=None, client=None, sleep=time.sleep, budget=None):
        self.config, self.clock = config, clock or WallClock()
        self.stop_event = Event()
        self._redactions = ()
        def interruptible_sleep(seconds):
            if sleep is time.sleep:
                if self.stop_event.wait(seconds):
                    raise DataError("Market-data request canceled")
            else:
                sleep(seconds)
        self.sleep = interruptible_sleep
        self.calendar = NYSE()
        if client is None:
            key, secret = credentials(root)
            self._redactions = (key, secret)
            client = StockHistoricalDataClient(key, secret)
            client._retry = 0  # Our transport handles retries and budgets per HTTP page.
            session = client._session
            original = session.request
            limiter = budget or Budget(config.requests_per_minute, sleep=self.sleep)

            def request(method, url, **kwargs):
                for attempt in range(config.max_retries + 1):
                    if self.stop_event.is_set():
                        raise DataError("Market-data request canceled")
                    limiter.acquire()
                    kwargs["timeout"] = config.timeout_seconds
                    try:
                        response = original(method, url, **kwargs)
                    except (requests.Timeout, requests.ConnectionError):
                        if attempt == config.max_retries:
                            raise DataError("Alpaca network timeout/connection failure; bounded retries exhausted") from None
                    else:
                        if response.status_code not in (429, 500, 502, 503, 504):
                            return response
                        if attempt == config.max_retries:
                            return response
                    self.sleep(min(2 ** attempt, 8))
                raise AssertionError("unreachable")
            session.request = request
        self.client = client

    def fetch(self, symbols, start: datetime, end: datetime):
        start, requested_end = utc(start), utc(end)
        end = min(requested_end, safe_cutoff(self.clock, self.config))
        if start >= end or end - start < timedelta(minutes=self.config.interval_minutes):
            raise DataError("Empty/invalid range after applying delayed SIP cutoff")
        if end != requested_end:
            print(f"SIP range shortened: {start.isoformat()} through {end.isoformat()} (requested {requested_end.isoformat()})")
        request = StockBarsRequest(symbol_or_symbols=list(symbols), start=start, end=end,
                                  timeframe=TimeFrame(self.config.interval_minutes, TimeFrameUnit.Minute),
                                  feed=DataFeed.SIP, adjustment=Adjustment.RAW)
        # This SDK normalizes inputs to naive UTC in its constructor. Restore
        # awareness as well as verifying the serialized HTTP fields in tests.
        request.start, request.end = start, end
        try:
            # alpaca-py 0.44.0: get_stock_bars -> _get_marketdata, loops next_page_token;
            # limit=None is deliberate: a total limit would truncate across symbols.
            response = self.client.get_stock_bars(request)
        except DataError:
            raise
        except Exception as exc:
            # APIError stores the actual server body, not credential headers.
            from alpaca.common.exceptions import APIError
            if isinstance(exc, APIError):
                message = str(exc)
                for secret in self._redactions:
                    message = message.replace(secret, "[REDACTED]")
                error = FatalDataError if exc.status_code in (401, 403, 422) else DataError
                raise error(f"Alpaca HTTP {exc.status_code}: {message}") from None
            raise DataError(f"Alpaca request failed ({type(exc).__name__})") from None
        delta = timedelta(minutes=self.config.interval_minutes)
        bars = []
        for symbol in symbols:
            for item in response.data.get(symbol, []):
                at = utc(item.timestamp)
                if at < start or at + delta > end:
                    continue
                if not self.calendar.session_for(at, at + delta):
                    continue  # Explicit regular-session filter; no extended-session fills.
                bars.append(Bar(symbol, at, at + delta, *(Decimal(str(getattr(item, key)))
                            for key in ("open", "high", "low", "close", "volume"))))
        validate_bars(bars, symbols, self.config.interval_minutes, self.calendar)
        order = {s: i for i, s in enumerate(symbols)}
        bars.sort(key=lambda b: (b.end, order[b.symbol]))
        return bars, end

    def close(self):
        if hasattr(self.client, "_session"):
            self.client._session.close()


def write_dataset(path: Path, bars, metadata: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(Bar.__dataclass_fields__)
    if path.suffix == ".parquet":
        import pandas as pd
        pd.DataFrame([b.row() for b in bars], columns=fields).to_parquet(path, index=False)
    else:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(b.row() for b in bars)
    metadata = {**metadata, "version": metadata.get("version", 1), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    path.with_suffix(path.suffix + ".json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def load_dataset(path: Path):
    try:
        meta = json.loads(path.with_suffix(path.suffix + ".json").read_text(encoding="utf-8"))
        if meta.get("provider") == "Yahoo":
            from .yahoo import load_yahoo_dataset
            return load_yahoo_dataset(path, meta)
        required = {"version", "sha256", "symbols", "interval_minutes", "provider", "feed", "adjustment",
                    "retrieved_at", "requested_start", "requested_end", "effective_start", "effective_end", "coverage"}
        if not required <= meta.keys() or meta["version"] != 1:
            raise DataError("Missing or unsupported dataset metadata")
        if meta["adjustment"] != "raw" or meta["feed"] not in ("sip", "SYNTHETIC"):
            raise DataError("Only explicitly raw SIP or SYNTHETIC datasets are supported")
        if (meta["feed"] == "sip" and meta["provider"] != "Alpaca") or (meta["feed"] == "SYNTHETIC" and meta["provider"] != "SYNTHETIC"):
            raise DataError("Provider/feed mismatch")
        if hashlib.sha256(path.read_bytes()).hexdigest() != meta["sha256"]:
            raise DataError("Dataset hash mismatch")
        minutes = meta["interval_minutes"]
        if minutes not in (1, 5, 15) or len(set(meta["symbols"])) != len(meta["symbols"]) or not meta["symbols"]:
            raise DataError("Invalid symbol/interval metadata")
        start, end = utc(meta["effective_start"]), utc(meta["effective_end"])
        if not utc(meta["requested_start"]) <= start < end <= utc(meta["requested_end"]):
            raise DataError("Invalid effective range metadata")
        utc(meta["retrieved_at"])
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not set(Bar.__dataclass_fields__) <= set(reader.fieldnames or []):
                raise DataError("Missing CSV columns")
            bars = [Bar(row["symbol"], utc(row["start"]), utc(row["end"]),
                        *(Decimal(row[k]) for k in ("open", "high", "low", "close", "volume"))) for row in reader]
        validate_bars(bars, meta["symbols"], minutes)
        if not bars or any(b.start < start or b.end > end for b in bars):
            raise DataError("Empty dataset or bars outside metadata range")
        order = {s: i for i, s in enumerate(meta["symbols"])}
        bars.sort(key=lambda b: (b.end, order[b.symbol]))
        actual = coverage(bars, meta["symbols"], start, end, minutes)
        if meta["coverage"] != actual:
            raise DataError("Coverage metadata mismatch")
        return bars, meta
    except DataError:
        raise
    except (OSError, ValueError, KeyError, ArithmeticError, TypeError) as exc:
        raise DataError(f"Invalid dataset: {exc}") from None


def metadata(config, bars, start, requested_end, end, clock, synthetic=False):
    return {"symbols": list(config.symbols), "interval_minutes": config.interval_minutes,
            "provider": "SYNTHETIC" if synthetic else "Alpaca", "feed": "SYNTHETIC" if synthetic else "sip",
            "adjustment": "raw", "retrieved_at": utc(clock.now()).isoformat(),
            "requested_start": utc(start).isoformat(), "requested_end": utc(requested_end).isoformat(),
            "effective_start": utc(start).isoformat(), "effective_end": utc(end).isoformat(),
            "coverage": coverage(bars, config.symbols, start, end, config.interval_minutes)}


def download(provider, config, start, end, output: Path, refresh=False):
    start, end = utc(start), utc(end)
    effective = min(end, safe_cutoff(provider.clock, config))
    if start >= effective or effective - start < timedelta(minutes=config.interval_minutes):
        raise DataError("Empty/invalid download range")
    identity = {"symbols": list(config.symbols), "interval_minutes": config.interval_minutes,
                "requested_start": start.isoformat(), "requested_end": end.isoformat(),
                "effective_end": effective.isoformat()}
    if output.exists() and not refresh:
        bars, meta = load_dataset(output)
        if meta["feed"] == "sip" and all(meta[k] == v for k, v in identity.items()):
            print(f"Cache hit: {output}; effective range {start.isoformat()} through {effective.isoformat()}")
            return bars, meta
        raise DataError("Output already exists with another identity; choose a new output or --refresh")
    bars, effective = provider.fetch(config.symbols, start, end)
    if not bars:
        raise DataError("No regular-session bars returned; no substitute data generated")
    meta = write_dataset(output, bars, metadata(config, bars, start, end, effective, provider.clock))
    return bars, meta
