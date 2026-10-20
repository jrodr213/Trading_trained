"""Offline Yahoo integration tests; every history response is a controlled frame."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import json
import numpy as np
import pandas as pd
import pytest
from gymnasium.utils.env_checker import check_env
from yfinance.exceptions import YFRateLimitError

from market_env import Config, MarketEnvironment, ReplayGymEnv
from market_env.clock import FrozenClock, utc
from market_env.data import DataError, FatalDataError, load_dataset, write_dataset
from market_env.providers import make_provider
from market_env.yahoo import (ADJUSTMENT, YahooProvider, YahooCalendar, yahoo_time, security_metadata,
                              normalize_history, download_yahoo, validate_yahoo_bars)
from market_env.yahoo_follow import YahooFollower
from market_env.__main__ import main


RAW_META = {"exchangeName": "PNK", "fullExchangeName": "OTC Markets OTCPK", "currency": "USD",
            "exchangeTimezoneName": "America/New_York", "firstTradeDate": pd.Timestamp("1996-11-18", tz="America/New_York")}


def daily_frame():
    times = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]).tz_localize("America/New_York")
    return pd.DataFrame({"Open": [10., 11., 12., 13.], "High": [11., 12., 13., 14.],
                         "Low": [9., 10., 11., 12.], "Close": [10.5, 11.5, 12.5, 13.5],
                         "Adj Close": [5., 6., 7., 8.], "Volume": [10000.] * 4,
                         "Dividends": [0., .5, 0., 0.], "Stock Splits": [0., 0., 2., 0.]}, index=times)


def intraday_frame():
    frame = daily_frame().iloc[:3].copy()
    frame.index = pd.date_range("2026-10-19T13:30:00Z", periods=3, freq="5min")
    frame["Stock Splits"] = 0.
    frame["Dividends"] = 0.
    return frame


class Ticker:
    def __init__(self, frame=None, error=None, metadata=None):
        self.frame, self.error = daily_frame() if frame is None else frame, error
        self.history_metadata = dict(RAW_META if metadata is None else metadata)
        self.calls = []

    def history(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.frame.copy()


def provider(tmp_path, interval="1d", tickers=None, clock=None):
    config = replace(Config(), provider="yahoo", symbols=("NTDOY",), yahoo_interval=interval,
                     data_dir=str(tmp_path / "data"), runs_dir=str(tmp_path / "runs"))
    tickers = tickers or {"NTDOY": Ticker()}
    clock = clock or FrozenClock(utc("2026-10-20T14:00:00Z"))
    return YahooProvider(config, tmp_path, clock, ticker_factory=lambda symbol: tickers[symbol]), config


def dataset(tmp_path, interval="1d", ticker=None):
    p, config = provider(tmp_path, interval, {"NTDOY": ticker or Ticker()})
    start, end = utc("2025-01-01T00:00Z"), utc("2025-01-08T00:00Z")
    bars, effective = p.fetch(config.symbols, start, end)
    return bars, p.dataset_metadata(bars, config.symbols, start, end, effective), config


def test_selection_default_preserved(tmp_path):
    with pytest.raises(FatalDataError, match="Missing"):
        make_provider(Config(), tmp_path)
    yahoo = make_provider(replace(Config(), provider="yahoo"), tmp_path)
    assert isinstance(yahoo, YahooProvider) and not yahoo.tickers


def test_daily_normalization_explicit_adjustment_actions_metadata(tmp_path):
    ticker = Ticker()
    p, config = provider(tmp_path, tickers={"NTDOY": ticker})
    bars, end = p.fetch(config.symbols, yahoo_time("2025-01-01"), yahoo_time("2025-01-08"))
    assert len(bars) == 4 and bars[0].start == utc("2025-01-02T14:30Z")
    assert bars[0].end == utc("2025-01-02T21:00Z")
    assert bars[0].close == Decimal("10.5")  # Never executable Adj Close=5.
    request = ticker.calls[0]
    assert request["auto_adjust"] is False and request["back_adjust"] is False and request["actions"]
    assert request["repair"] is False and request["timeout"] == 15
    assert request["start"].tzinfo and request["end"].tzinfo
    meta = p.dataset_metadata(bars, config.symbols, request["start"], request["end"], end)
    assert {a["kind"] for a in meta["corporate_actions"]["NTDOY"]} == {"split", "dividend"}
    assert meta["securities"]["NTDOY"]["is_otc"] is True
    json.dumps(meta)  # pandas firstTradeDate timestamps must be serialized explicitly.


@pytest.mark.parametrize("interval,index,end,expected_end", [
    ("1wk", "2025-01-06", "2025-01-11", "2025-01-10T21:00Z"),
    ("1mo", "2025-01-01", "2025-02-01", "2025-01-31T21:00Z")])
def test_weekly_monthly_bounds(interval, index, end, expected_end):
    frame = daily_frame().iloc[:1].copy()
    frame.index = pd.DatetimeIndex([index], tz="America/New_York")
    bars, _, _ = normalize_history("NTDOY", frame, interval, security_metadata(RAW_META), yahoo_time("2025-01-01"), yahoo_time(end))
    assert len(bars) == 1 and bars[0].end == utc(expected_end)
    with pytest.raises(DataError, match="completed"):
        normalize_history("NTDOY", frame, interval, security_metadata(RAW_META), yahoo_time("2025-01-01"), yahoo_time("2025-01-08"))


def test_intraday_normalization_and_complete_bar_cutoff(tmp_path):
    clock = FrozenClock(utc("2026-10-19T13:56:00Z"))  # cutoff 13:40:30
    ticker = Ticker(intraday_frame())
    p, config = provider(tmp_path, "5m", {"NTDOY": ticker}, clock)
    bars, end = p.fetch(config.symbols, utc("2026-10-19T13:30Z"), utc("2026-10-19T14:00Z"))
    assert [b.end for b in bars] == [utc("2026-10-19T13:35Z"), utc("2026-10-19T13:40Z")]
    assert end == utc("2026-10-19T13:40:30Z")
    assert ticker.calls[0]["interval"] == "5m"


@pytest.mark.parametrize("exchange,expected", [("PNK", True), ("OQX", True), ("NMS", False), ("NOT_KNOWN", None)])
def test_otc_classification_not_assumed(exchange, expected):
    assert security_metadata({"exchangeName": exchange})["is_otc"] is expected


def test_timestamp_conversion_duplicates_and_missing_volume():
    frame = pd.concat([intraday_frame().iloc[:1], intraday_frame().iloc[:1]])
    frame.index = pd.DatetimeIndex(["2026-10-19T09:30", "2026-10-19T09:30"])
    frame["Volume"] = np.nan
    bars, _, diag = normalize_history("NTDOY", frame, "5m", security_metadata(RAW_META), utc("2026-10-19T13:00Z"), utc("2026-10-19T14:00Z"))
    assert len(bars) == 1 and bars[0].start == utc("2026-10-19T13:30Z")
    assert bars[0].volume.is_nan() and "duplicate" in diag[0]
    with pytest.raises(DataError, match="timezone"):
        normalize_history("NTDOY", frame, "5m", {}, utc("2026-10-19T13:00Z"), utc("2026-10-19T14:00Z"))


@pytest.mark.parametrize("bad", [pd.DataFrame(), daily_frame().drop(columns=["High"]),
    daily_frame().assign(Open=-1), daily_frame().assign(Volume=-1), daily_frame().assign(Close=np.inf)])
def test_invalid_and_empty_response_reported(tmp_path, bad):
    p, config = provider(tmp_path, tickers={"NTDOY": Ticker(bad)})
    bars, _ = p.fetch(config.symbols, yahoo_time("2025-01-01"), yahoo_time("2025-01-08"))
    assert not bars and p.reports["NTDOY"]["status"] == "request_failed"


def test_missing_ticker_does_not_block_other_tickers_or_limit_history(tmp_path):
    good, bad = Ticker(), Ticker(pd.DataFrame())
    p, config = provider(tmp_path, tickers={"NTDOY": good, "MISSING": bad})
    bars, _ = p.fetch(("MISSING", "NTDOY"), yahoo_time("1990-01-01"), yahoo_time("2025-01-08"))
    assert len(bars) == 4 and p.reports["MISSING"]["error"]
    assert good.calls[0]["start"].year == 1990


def test_intraday_restriction_no_silent_daily_fallback(tmp_path):
    ticker = Ticker()
    p, config = provider(tmp_path, "1m", {"NTDOY": ticker})
    with pytest.raises(DataError, match="Use --interval 1d"):
        p.fetch(config.symbols, yahoo_time("2020-01-01"), yahoo_time("2025-01-01"))
    assert not ticker.calls


def test_rate_limit_backoff_clean_recovery(tmp_path):
    clock = FrozenClock(utc("2026-10-20T14:00Z"))
    ticker = Ticker(error=YFRateLimitError())
    p, config = provider(tmp_path, tickers={"NTDOY": ticker}, clock=clock)
    args = (config.symbols, yahoo_time("2025-01-01"), yahoo_time("2025-01-08"))
    assert not p.fetch(*args)[0] and p.reports["NTDOY"]["status"] == "rate_limited"
    assert not p.fetch(*args)[0] and len(ticker.calls) == 1 and p.reports["NTDOY"]["status"] == "backoff"
    clock.value += timedelta(seconds=121)
    ticker.error = None
    assert len(p.fetch(*args)[0]) == 4 and len(ticker.calls) == 2


def test_csv_parquet_cache_source_and_identity(tmp_path):
    ticker = Ticker()
    p, config = provider(tmp_path, tickers={"NTDOY": ticker})
    out = tmp_path / "data"
    first = download_yahoo(p, config, yahoo_time("2025-01-01"), yahoo_time("2025-01-08"), out)
    assert first["NTDOY"]["status"] == "downloaded"
    csv_bars, csv_meta = load_dataset(out / "NTDOY.csv")
    pq_bars, pq_meta = load_dataset(out / "NTDOY.parquet")
    assert csv_bars == pq_bars and csv_meta["provider"] == pq_meta["provider"] == "Yahoo"
    assert len(list((out / "sources").glob("*.csv"))) == 1
    second = download_yahoo(p, config, yahoo_time("2025-01-01"), yahoo_time("2025-01-08"), out)
    assert second["NTDOY"]["status"] == "cached" and len(ticker.calls) == 1
    with pytest.raises(DataError, match="another identity"):
        download_yahoo(p, config, yahoo_time("2024-01-01"), yahoo_time("2025-01-08"), out)


def test_yahoo_replay_shared_simulator_accounting_no_lookahead(tmp_path):
    bars, meta, config = dataset(tmp_path)
    config = replace(config, fee_per_order="1", slippage_bps="100")
    env = MarketEnvironment(bars, config, dataset=meta)
    assert env.get_orders() == [] and len(env.get_observation()["history"]["NTDOY"]) == 1
    assert env.submit_order("NTDOY", "buy", 10)["status"] == "pending"
    env.advance_replay()
    fill = env.sim.fills[0]
    assert fill["price"] == Decimal("11.110000") and fill["market_time"] == bars[1].start
    assert "ESTIMATED" in fill["execution_quality"]
    assert env.get_portfolio()["cash"] == Decimal("99887.90") and env.get_portfolio()["fees"] == 1
    assert env.get_portfolio()["equity"] == Decimal("100002.90")
    # Recorded dividends/splits are not reapplied to split-normalized price/share units.
    assert env.submit_order("NTDOY", "sell", 4)["status"] == "pending"
    env.advance_replay()
    assert env.get_portfolio()["holdings"]["NTDOY"] == 6
    assert env.get_portfolio()["cash"] == Decimal("99934.42")
    assert env.get_portfolio()["realized_pnl"] == Decimal("1.68")
    exported = env.export_run(tmp_path / "runs")
    observed, identity = load_dataset(exported / "observed.csv")
    assert len(observed) == 3 and identity["provider"] == "Yahoo"
    assert json.loads((exported / "limitations.json").read_text())[0].startswith("Yahoo")


def test_yahoo_gym_validation_reward_and_missing_volume_mask(tmp_path):
    frame = daily_frame()
    frame.iloc[0, frame.columns.get_loc("Volume")] = np.nan
    bars, meta, config = dataset(tmp_path, ticker=Ticker(frame))
    env = ReplayGymEnv(bars, config, dataset=meta)
    check_env(env, skip_render_check=True)
    obs, _ = env.reset(seed=42)
    assert obs["volume_mask"][-1, 0] == 0 and obs["history"][-1, 0, 4] == 0
    _, reward, *_ = env.step(np.array([1], dtype=np.int64))
    assert reward == 0 and not env.core.sim.fills  # Unknown previous volume -> stays pending.


def test_liquidity_capacity_causal_and_subcent_prices(tmp_path):
    frame = daily_frame()
    frame[["Open", "High", "Low", "Close"]] /= 1000
    frame["Volume"] = [10., 10000., 10000., 10000.]
    bars, meta, config = dataset(tmp_path, ticker=Ticker(frame))
    env = MarketEnvironment(bars, config, dataset=meta)
    env.submit_order("NTDOY", "buy", 5)
    env.advance_replay()
    assert not env.sim.fills  # Prior volume=10 permits only 1, despite next volume=10000.
    env.advance_replay()
    assert env.sim.fills[0]["price"] == Decimal("0.012000")
    assert env.get_portfolio()["cash"] == Decimal("99999.94")


def test_non_usd_download_allowed_but_no_silent_fx_accounting(tmp_path):
    bars, meta, config = dataset(tmp_path, ticker=Ticker(metadata={**RAW_META, "currency": "CHF"}))
    with pytest.raises(DataError, match="USD"):
        MarketEnvironment(bars, config, dataset=meta)


def follow_setup(tmp_path):
    ticker = Ticker(intraday_frame())
    clock = FrozenClock(utc("2026-10-19T13:51Z"))  # 13:35:30 cutoff, one 5min bar.
    p, config = provider(tmp_path, "5m", {"NTDOY": ticker}, clock)
    meta = p.dataset_metadata([], config.symbols, utc("2026-10-19T13:30Z"), utc("2026-10-19T14:00Z"), utc("2026-10-19T13:30:01Z"))
    env = MarketEnvironment([], config, clock, meta, follow=True)
    follower = YahooFollower(env, p, start=utc("2026-10-19T13:30Z"))
    return env, p, ticker, follower, clock


def test_follow_delay_dedup_incremental_and_manual_only(tmp_path):
    env, p, ticker, follower, clock = follow_setup(tmp_path)
    follower.poll_once()
    assert not env.get_orders() and env.get_portfolio()["cash"] == 100000
    status = env.diagnostics()
    assert status["label"].startswith("DELAYED OTC MARKET")
    assert status["symbols"]["NTDOY"]["data_age_seconds"] == 960
    assert status["symbols"]["NTDOY"]["unexpected_lag_seconds"] == 0
    assert not status["symbols"]["NTDOY"]["stale"]
    env.submit_order("NTDOY", "buy", 5)
    follower.poll_once()
    assert not env.sim.fills
    clock.value += timedelta(minutes=10)
    follower.poll_once()
    assert len(env.sim.fills) == 1 and env.sim.fills[0]["market_time"] == utc("2026-10-19T13:35Z")
    assert len(env.price_history["NTDOY"]) == 3
    follower.poll_once()
    assert len(env.sim.fills) == 1 and len(env.price_history["NTDOY"]) == 3
    assert ticker.calls[-1]["start"] > ticker.calls[0]["start"]
    clock.value += timedelta(minutes=10)
    assert env.diagnostics()["symbols"]["NTDOY"]["stale"]
    assert env.submit_order("NTDOY", "buy", 1)["status"] == "rejected"


def test_follow_failed_request_pauses_recovery_and_revisions(tmp_path):
    env, p, ticker, follower, clock = follow_setup(tmp_path)
    follower.poll_once()
    old = env.latest["NTDOY"]
    ticker.frame.iloc[0, ticker.frame.columns.get_loc("Close")] = 10.6
    follower.poll_once()
    assert env.latest["NTDOY"] == old and env.revisions
    ticker.error = YFRateLimitError()
    follower.poll_once()
    assert env.connection.startswith("paused") and env.data_status["NTDOY"]["status"] == "rate_limited"
    assert env.submit_order("NTDOY", "buy", 1)["status"] == "rejected"
    clock.value += timedelta(seconds=121)
    ticker.error = None
    follower.poll_once()
    assert env.connection == "connected" and not env.ended


def test_follow_split_basis_change_stops_before_fills(tmp_path):
    env, p, ticker, follower, clock = follow_setup(tmp_path)
    follower.poll_once()
    env.submit_order("NTDOY", "buy", 1)
    ticker.frame.iloc[1, ticker.frame.columns.get_loc("Stock Splits")] = 2
    clock.value += timedelta(minutes=5)
    follower.poll_once()
    assert env.ended and not env.sim.fills
    assert env.get_orders()[0]["status"] == "canceled" and "split" in env.connection


def test_cli_yahoo_download_coverage_replay_and_follow_selection(tmp_path, monkeypatch, capsys):
    import market_env.__main__ as cli
    p, config = provider(tmp_path)
    monkeypatch.setattr(cli, "make_provider", lambda cfg, root: p)
    assert main(["--project", str(tmp_path), "download", "--provider", "yahoo", "--symbols", "NTDOY",
                 "--start", "2025-01-01", "--end", "2025-01-08", "--interval", "1d", "--output", "data/otc"]) == 0
    assert main(["--project", str(tmp_path), "coverage", "--data", "data/otc/NTDOY.parquet"]) == 0
    commands = iter(["orders", "buy NTDOY 2", "next", "portfolio", "quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(commands))
    assert main(["--project", str(tmp_path), "replay", "--data", "data/otc/NTDOY.csv"]) == 0
    assert "yahoo / LOCAL SIMULATED REPLAY" in capsys.readouterr().out
    seen = []
    monkeypatch.setattr(cli, "console", lambda env, follower: seen.append((env, follower)))
    assert main(["--project", str(tmp_path), "follow", "--provider", "yahoo", "--symbols", "NTDOY", "--poll-interval", "180"]) == 0
    assert isinstance(seen[0][1], YahooFollower) and seen[0][1].config.poll_interval_seconds == 180


def test_declared_period_bounds_validated_on_load(tmp_path):
    bars, meta, _ = dataset(tmp_path)
    bad = [replace(bars[0], end=bars[0].start + timedelta(minutes=1))]
    path = tmp_path / "invalid.csv"
    write_dataset(path, bad, meta)
    with pytest.raises(DataError, match="complete interval"):
        load_dataset(path)


def test_otc_actual_bar_not_discarded_for_reference_holiday():
    frame = daily_frame().iloc[:1].copy()
    frame.index = pd.DatetimeIndex(["2025-01-01"], tz="America/New_York")
    bars, _, _ = normalize_history("NTDOY", frame, "1d", security_metadata(RAW_META),
                                   utc("2025-01-01T00:00Z"), utc("2025-01-03T00:00Z"))
    assert len(bars) == 1 and bars[0].end == utc("2025-01-02T05:00Z")


def test_duplicate_missing_volume_is_not_a_vendor_revision(tmp_path):
    env, p, ticker, follower, clock = follow_setup(tmp_path)
    ticker.frame["Volume"] = np.nan
    follower.poll_once()
    follower.poll_once()
    assert len(env.price_history["NTDOY"]) == 1 and not env.revisions
    assert env.diagnostics()["symbols"]["NTDOY"]["volume_warning"]


def test_future_out_of_range_actions_not_recorded(tmp_path):
    p, config = provider(tmp_path)
    bars, end = p.fetch(config.symbols, yahoo_time("2025-01-01"), yahoo_time("2025-01-03"))
    assert len(bars) == 1 and p.actions["NTDOY"] == []


def test_stop_interrupts_budget_wait(tmp_path):
    p, _ = provider(tmp_path)
    p.stop_event.set()
    with pytest.raises(DataError, match="canceled"):
        p.sleep(0)
