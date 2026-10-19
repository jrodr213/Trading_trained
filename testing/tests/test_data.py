from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
import json
import requests
import pytest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.enums import DataFeed, Adjustment

from market_env.clock import FrozenClock, utc, safe_cutoff
from market_env.config import Config
from market_env.data import (AlpacaProvider, DataError, FatalDataError, Budget,
                             load_dataset, write_dataset, metadata, download, validate_bars, credentials)
from market_env.replay import synthetic_bars, create_synthetic


def response(status=200, body=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body or {"bars": {}, "next_page_token": None}).encode()
    return result


def sdk_bar(at="2026-09-01T13:30:00Z", price=60):
    return {"t": at, "o": price, "h": price + 1, "l": price - 1, "c": price, "v": 100, "n": 10, "vw": price}


def provider_with_http(tmp_path, monkeypatch, handler, config=None, sleep=lambda _: None):
    monkeypatch.setenv("ALPACA_API_KEY", "TEST_KEY_NEVER_REAL")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "TEST_SECRET_NEVER_REAL")
    monkeypatch.setattr(requests.Session, "request", handler)
    return AlpacaProvider(config or Config(), tmp_path,
                          clock=FrozenClock(utc("2026-09-01T13:47:00Z")), sleep=sleep)


def test_sip_cutoff_full_interval_and_timezone(tmp_path, capsys):
    captured = []
    def get(request):
        captured.append(request)
        raw = synthetic_bars()
        return SimpleNamespace(data={s: [SimpleNamespace(timestamp=b.start, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume)
                                          for b in raw if b.symbol == s] for s in ("KO", "JPM", "IBM")})
    clock = FrozenClock(utc("2026-09-01T13:47:00Z"))  # cutoff 13:31:30
    provider = AlpacaProvider(Config(), tmp_path, clock, client=SimpleNamespace(get_stock_bars=get))
    bars, end = provider.fetch(Config().symbols, utc("2026-09-01T09:30:00-04:00"), utc("2026-09-01T14:00:00Z"))
    request = captured[0]
    assert request.feed == DataFeed.SIP and request.adjustment == Adjustment.RAW
    assert request.start == utc("2026-09-01T13:30:00Z")
    assert request.end == end == safe_cutoff(clock, Config())
    assert request.limit is None and request.end.tzinfo is not None
    assert len(bars) == 3 and all(b.end <= end for b in bars)
    assert all(b.start.minute == 30 for b in bars)  # The 13:31 bar is still incomplete.
    assert "shortened" in capsys.readouterr().out


@pytest.mark.parametrize("start,end", [("2026-09-01", "2026-09-02T00:00Z"),
                                                    ("2026-09-02T00:00Z", "2026-09-01T00:00Z"),
                                                    ("2026-09-01T13:32:00Z", "2026-09-01T14:00:00Z")])
def test_invalid_range_before_request(tmp_path, start, end):
    def fail(_):
        pytest.fail("Invalid range reached SDK")
    p = AlpacaProvider(Config(), tmp_path, FrozenClock(utc("2026-09-01T13:47:00Z")), client=SimpleNamespace(get_stock_bars=fail))
    with pytest.raises((ValueError, DataError)):
        p.fetch(("KO",), start, end)


def test_installed_sdk_pagination_across_symbols(tmp_path, monkeypatch):
    calls = []
    pages = [{"bars": {"KO": [sdk_bar()]}, "next_page_token": "second"},
             {"bars": {"JPM": [sdk_bar(price=130)], "IBM": [sdk_bar(price=200)]}, "next_page_token": None}]
    def handler(session, method, url, **kw):
        calls.append((method, url, dict(kw["params"]), kw["timeout"]))
        return response(body=pages[len(calls) - 1])
    p = provider_with_http(tmp_path, monkeypatch, handler)
    bars, _ = p.fetch(Config().symbols, utc("2026-09-01T13:30Z"), utc("2026-09-01T13:31Z"))
    assert isinstance(p.client, StockHistoricalDataClient)
    assert [b.symbol for b in bars] == ["KO", "JPM", "IBM"]
    assert len(calls) == 2 and calls[1][2]["page_token"] == "second"
    assert calls[0][2]["feed"] == "sip" and calls[0][2]["limit"] == 10000
    assert calls[0][2]["start"].endswith("+00:00") and calls[0][2]["end"].endswith("+00:00")
    assert all(method == "GET" and url.endswith("/stocks/bars") and timeout == 15 for method, url, _, timeout in calls)


@pytest.mark.parametrize("status", [401, 403, 422])
def test_auth_entitlement_no_retry_or_fallback(tmp_path, monkeypatch, status):
    calls = []
    def handler(*args, **kw):
        calls.append(kw)
        return response(status, {"code": status, "message": "subscription does not permit querying recent SIP data"})
    p = provider_with_http(tmp_path, monkeypatch, handler)
    with pytest.raises(FatalDataError, match="subscription does not permit"):
        p.fetch(("KO",), utc("2026-09-01T13:30Z"), utc("2026-09-01T13:31Z"))
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [429, 503, "timeout"])
def test_bounded_retries_and_timeout(tmp_path, monkeypatch, failure):
    calls, sleeps = [], []
    def handler(*args, **kw):
        calls.append(kw)
        if failure == "timeout":
            raise requests.Timeout()
        return response(failure, {"code": failure, "message": "temporarily unavailable"})
    p = provider_with_http(tmp_path, monkeypatch, handler, replace(Config(), max_retries=2), sleeps.append)
    with pytest.raises(DataError):
        p.fetch(("KO",), utc("2026-09-01T13:30Z"), utc("2026-09-01T13:31Z"))
    assert len(calls) == 3 and sleeps == [1, 2]
    assert all(kw["timeout"] == 15 for kw in calls)


def test_retry_recovery_budget_each_page_and_attempt(tmp_path, monkeypatch):
    attempts, budget_calls = [], []
    class CountingBudget:
        def acquire(self):
            budget_calls.append(1)
    def handler(*args, **kwargs):
        attempts.append(1)
        return response(429, {"message": "retry"}) if len(attempts) == 1 else response()
    monkeypatch.setenv("ALPACA_API_KEY", "x")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "y")
    monkeypatch.setattr(requests.Session, "request", handler)
    p = AlpacaProvider(Config(), tmp_path, FrozenClock(utc("2026-09-01T14:00Z")), sleep=lambda _: None, budget=CountingBudget())
    assert p.fetch(("KO",), utc("2026-09-01T13:30Z"), utc("2026-09-01T13:31Z"))[0] == []
    assert len(budget_calls) == 2


def test_sliding_window_budget():
    now, sleeps = [0.], []
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    budget = Budget(2, lambda: now[0], sleep)
    budget.acquire(); budget.acquire(); budget.acquire()
    assert sleeps == [60.] and list(budget.attempts) == [60.]


def test_credentials_exact_project_only(tmp_path):
    (tmp_path / ".env").write_text("ALPACA_API_KEY=local\nALPACA_SECRET_KEY=secret\n")
    child = tmp_path / "child"
    child.mkdir()
    with pytest.raises(FatalDataError, match="Missing"):
        credentials(child)
    assert credentials(tmp_path) == ("local", "secret")


def test_dataset_roundtrip_hash_and_cache(tmp_path):
    path = tmp_path / "SYNTHETIC.csv"
    bars, meta = create_synthetic(path)
    assert load_dataset(path) == (bars, meta)
    path.write_text(path.read_text() + "\n")
    with pytest.raises(DataError, match="hash"):
        load_dataset(path)


def test_download_cache_coverage_missing_symbol(tmp_path):
    config = Config()
    bars = [b for b in synthetic_bars() if b.symbol != "IBM"]
    calls = []
    start, end = bars[0].start, bars[-1].end
    class Provider:
        clock = FrozenClock(utc("2026-09-02T00:00Z"))
        def fetch(self, *args):
            calls.append(args)
            return bars, end
    path = tmp_path / "history.csv"
    _, meta = download(Provider(), config, start, end, path)
    assert meta["coverage"]["IBM"]["bars"] == 0
    assert meta["coverage"]["IBM"]["missing_expected_bars"] == 8
    download(Provider(), config, start, end, path)
    assert len(calls) == 1


@pytest.mark.parametrize("change", [dict(open=Decimal("NaN")), dict(close=Decimal("-1")),
                                    dict(high=Decimal("1")), dict(volume=Decimal("-1")),
                                    dict(start=utc("2026-09-01T13:30:01Z")),
                                    dict(end=utc("2026-09-01T13:30:30Z"))])
def test_invalid_bars(change):
    with pytest.raises(DataError):
        validate_bars([replace(synthetic_bars()[0], **change)], Config().symbols, 1)


def test_duplicate_rows():
    with pytest.raises(DataError, match="Duplicate"):
        validate_bars([synthetic_bars()[0]] * 2, Config().symbols, 1)


def test_adjusted_data_rejected(tmp_path):
    path = tmp_path / "data.csv"
    bars, meta = create_synthetic(path)
    meta["adjustment"] = "split"
    write_dataset(path, bars, meta)
    with pytest.raises(DataError, match="raw"):
        load_dataset(path)


@pytest.mark.parametrize("minutes", [5, 15])
def test_supported_intervals_require_full_completion(tmp_path, minutes):
    captured = []
    def get(req):
        captured.append(req)
        return SimpleNamespace(data={"KO": [SimpleNamespace(timestamp=utc("2026-09-01T13:30Z"), open=60, high=61, low=59, close=60, volume=100)]})
    config = replace(Config(), interval_minutes=minutes)
    clock = FrozenClock(utc("2026-09-01T14:00Z"))  # cutoff 13:44:30
    p = AlpacaProvider(config, tmp_path, clock, client=SimpleNamespace(get_stock_bars=get))
    bars, _ = p.fetch(("KO",), utc("2026-09-01T13:00Z"), utc("2026-09-01T14:00Z"))
    assert len(bars) == (1 if minutes == 5 else 0)
    assert captured[0].timeframe.amount == minutes
    clock.value += timedelta(minutes=1)
    assert len(p.fetch(("KO",), utc("2026-09-01T13:00Z"), utc("2026-09-01T14:00Z"))[0]) == 1


def test_tracked_synthetic_fixture():
    from pathlib import Path
    bars, identity = load_dataset(Path(__file__).parent / "fixtures" / "SYNTHETIC.csv")
    assert bars == synthetic_bars() and identity["feed"] == "SYNTHETIC"
