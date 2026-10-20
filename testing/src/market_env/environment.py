from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Callable
import numpy as np
import gymnasium as gym
from gymnasium import spaces

from .clock import Clock, WallClock, NYSE, safe_cutoff
from .config import Config
from .data import Bar, DataError, validate_bars
from .replay import groups
from .simulator import Simulator


class MarketEnvironment:
    """Thread-safe manual/external API, with one raw-price NYSE session per account."""
    def __init__(self, bars: list[Bar], config: Config | None = None, clock: Clock | None = None,
                 dataset: dict | None = None, follow: bool = False):
        self.config, self.clock = config or Config(), clock or WallClock()
        self.dataset = dict(dataset or {"provider": "SYNTHETIC", "feed": "SYNTHETIC"})
        self.yahoo = self.dataset.get("provider") == "Yahoo"
        self.lock = RLock()
        if self.yahoo:
            from .yahoo import dataset_calendar, validate_yahoo_bars, ADJUSTMENT
            self.config = replace(self.config, provider="yahoo", yahoo_interval=self.dataset["interval"],
                                  execution_price_tick=self.config.yahoo_execution_price_tick)
            self.calendar = dataset_calendar(self.dataset)
            if self.dataset.get("adjustment") != ADJUSTMENT:
                raise DataError("Yahoo requires an explicit fixed split-normalized share basis; dividend-adjusted prices are unsupported")
            currencies = {s.get("currency") for s in self.dataset.get("securities", {}).values()}
            if (currencies - {"USD"} or set(self.config.symbols) - self.dataset.get("securities", {}).keys()) and (bars or not follow):
                raise DataError("Yahoo trading requires known USD currency; foreign-currency data can be downloaded but FX accounting is unsupported")
            calendars = {(s.get("calendar"), s.get("timezone")) for s in self.dataset.get("securities", {}).values()}
            if len(calendars) > 1:
                raise DataError("Replay one Yahoo calendar/timezone per account")
            validate_yahoo_bars(bars, self.config.symbols, self.config.yahoo_interval, self.dataset.get("securities", {}))
        else:
            self.calendar = NYSE()
        self.follow = follow
        if not self.yahoo:
            validate_bars(bars, self.config.symbols, self.config.interval_minutes, self.calendar)
        if not self.yahoo and self.dataset.get("adjustment", "raw") != "raw":
            raise DataError("Raw single-session accounting required")
        sessions = set() if self.yahoo else {self.calendar.session_for(b.start, b.end) for b in bars}
        if len(sessions) > 1 and not self.yahoo:
            raise DataError("Raw replay is limited to ONE NYSE session to exclude overnight corporate actions; select --session YYYY-MM-DD")
        self._bars = list(bars)
        self._groups = groups(bars, self.config.symbols)
        self.reset()

    def reset(self) -> dict:
        with self.lock:
            if self.follow and hasattr(self, "sim"):
                raise RuntimeError("Create a fresh follow environment instead of resetting a running poller")
            self.sim = Simulator(self.config, self.clock)
            self.market_time: datetime | None = None
            self.session: str | None = None
            self.latest: dict[str, Bar] = {}
            self.price_history: dict[str, list[Bar]] = {s: [] for s in self.config.symbols}
            self.connection = "waiting" if self.follow else "offline"
            self.ended, self.closed = False, False
            self.index = 0
            self.revisions: list[dict] = []
            self.acquisition: list[dict] = []
            self.data_status: dict[str, dict] = {}
            self._seen: dict[tuple, Bar] = {}
            if self._groups:
                self._process(self._groups[0])
                self.index = 1
                if self.index == len(self._groups):
                    self.finish("end of data")
            return self.get_observation()

    def _process(self, batch: list[Bar]):
        fresh = []
        for bar in batch:
            key = (bar.symbol, bar.start)
            if key in self._seen:
                if self._seen[key].row() != bar.row():
                    self.revisions.append({"kind": "vendor revision ignored", "wall_time": self.clock.now(),
                                           "original": self._seen[key].row(), "received": bar.row()})
                continue
            self._seen[key] = bar
            if self.market_time is not None and bar.end <= self.market_time:
                self.revisions.append({"kind": "late bar ignored for simulation", "wall_time": self.clock.now(), "bar": bar.row()})
                continue
            session = self.calendar.session_for(bar.start, bar.end)
            if self.session is not None and session != self.session and not self.yahoo:
                self.finish("session boundary: corporate actions unsupported")
                continue
            self.session = session
            fresh.append(bar)
        if not fresh or self.ended:
            return
        # Every pending fill sees only the OPEN; marks/observations follow all fills.
        for bar in fresh:
            self.sim.process_open(bar)
        for bar in fresh:
            self.sim.prices[bar.symbol] = bar.close
            self.sim.volumes[bar.symbol] = bar.volume
            self.latest[bar.symbol] = bar
            self.price_history[bar.symbol].append(bar)
        self.market_time = max(bar.end for bar in fresh)
        self.sim.history.append({"market_time": self.market_time, "wall_time": self.clock.now(), **self.sim.portfolio()})

    def ingest(self, bars: list[Bar]):
        with self.lock:
            if not self.follow:
                raise RuntimeError("ingest is available only in follow mode")
            if self.yahoo:
                from .yahoo import validate_yahoo_bars
                validate_yahoo_bars(bars, self.config.symbols, self.config.yahoo_interval, self.dataset.get("securities", {}))
                if any(self.dataset.get("securities", {}).get(b.symbol, {}).get("currency") != "USD" for b in bars):
                    raise DataError("Yahoo ingest requires known USD currency metadata")
            else:
                validate_bars(bars, self.config.symbols, self.config.interval_minutes, self.calendar)
            cutoff = safe_cutoff(self.clock, self.config)
            if any(b.end > cutoff for b in bars):
                raise DataError("Attempt to expose an incomplete/ineligible delayed bar")
            for batch in groups(bars, self.config.symbols):
                self._process(batch)
            if self.market_time is not None and not self.yahoo:
                session = next((s for s in self.calendar.around(self.market_time) if s[0] == self.session), None)
                if session and cutoff >= session[2] and self.market_time >= session[2]:
                    self.finish("delayed session complete")

    def finish(self, reason):
        self.ended = True
        self.sim.cancel_all(self.market_time, reason)

    def advance_replay(self) -> bool:
        with self.lock:
            if self.follow:
                raise RuntimeError("next advances replay only")
            if self.ended or self.closed:
                return False
            if self.index < len(self._groups):
                self._process(self._groups[self.index])
                self.index += 1
            if self.index >= len(self._groups):
                self.finish("end of data; no liquidation")
            return True

    def diagnostics(self) -> dict:
        with self.lock:
            wall = self.clock.now()
            cutoff = safe_cutoff(self.clock, self.config)
            from .yahoo import INTERVALS
            minutes = (INTERVALS[self.config.yahoo_interval] or 1440) if self.yahoo else self.config.interval_minutes
            expected = self.calendar.expected_end(cutoff, minutes) if self.follow else self.market_time
            by_symbol = {}
            for symbol in self.config.symbols:
                bar = self.latest.get(symbol)
                lag = max(0., (expected - bar.end).total_seconds()) if bar and expected else None
                by_symbol[symbol] = {"latest_completed_bar": bar.end if bar else None,
                                     "data_age_seconds": (wall - bar.end).total_seconds() if bar else None,
                                     "unexpected_lag_seconds": lag,
                                     "stale": bar is None or (lag is not None and lag > self.config.stale_tolerance_seconds)}
                if self.yahoo:
                    data = self.data_status.get(symbol, {})
                    volume = bar.volume if bar else None
                    by_symbol[symbol].update({"price": bar.close if bar else None, "provider": "Yahoo/yfinance",
                        "last_market_event": bar.end if bar else None, "wall_time": wall,
                        "timestamp_basis": "completed bar end; precise last-trade time is unavailable",
                        "data_status": data.get("status", "waiting"), "request_error": data.get("error"),
                        "activity": "zero volume reported (inactive interval)" if volume is not None and volume == 0 else
                                    ("unknown; no recent trades OR missing vendor coverage" if by_symbol[symbol]["stale"] else "reported trade volume"),
                        "volume_warning": "low or missing volume; fills are estimates" if volume is None or volume.is_nan() or volume < self.config.low_volume_threshold else None,
                        "calendar_basis": self.dataset.get("securities", {}).get(symbol, {}).get("calendar_basis")})
                    if data.get("status") not in (None, "ok"):
                        by_symbol[symbol]["stale"] = True
            return {"label": ("DELAYED OTC MARKET / LOCAL SIMULATED TRADES" if self.yahoo else "DELAYED SIP DATA / LOCAL SIMULATED TRADES") if self.follow else f"{self.dataset.get('feed')} / LOCAL SIMULATED REPLAY",
                    "wall_time": wall, "market_time": self.market_time, "expected_market_time": expected,
                    "intentional_delay_seconds": self.config.delay_minutes * 60 + self.config.safety_buffer_seconds,
                    "connection": self.connection, "wall_session_open": self.calendar.is_open(wall),
                    "market_session_open": self.calendar.is_open(self.market_time) if self.market_time else False,
                    "symbols": by_symbol, "ended": self.ended, "session": self.session}

    def submit_order(self, symbol: str, side: str, quantity: int, client_order_id: str | None = None) -> dict:
        with self.lock:
            diag = self.diagnostics()
            symbol_info = diag["symbols"].get(symbol, {})
            # Replay stale symbols cannot use an old mark from a missing event.
            bar = self.latest.get(symbol)
            available = bar is not None and self.market_time is not None and bar.end == self.market_time
            if self.follow:
                available = available and self.connection == "connected" and not symbol_info.get("stale", True)
            # Daily/weekly/monthly decisions queue at a completed period's close;
            # the next period's OPEN is later, even though the exchange is now closed.
            session_allows = self.yahoo or diag["market_session_open"]
            allowed = available and session_allows and not self.ended and not self.closed
            return asdict(self.sim.submit(symbol, side, quantity, self.market_time, allowed, client_order_id))

    def cancel_order(self, order_id: str) -> bool:
        with self.lock:
            return self.sim.cancel(order_id, self.market_time)

    def get_portfolio(self) -> dict:
        with self.lock:
            return self.sim.portfolio()

    def get_orders(self) -> list[dict]:
        with self.lock:
            return [asdict(o) for o in self.sim.orders.values()]

    def get_observation(self) -> dict:
        with self.lock:
            return {"symbols": self.config.symbols, "market_time": self.market_time,
                    "history": {s: tuple(v) for s, v in self.price_history.items()},
                    "missing": {s: s not in self.latest or self.latest[s].end != self.market_time for s in self.config.symbols},
                    "portfolio": self.sim.portfolio(), "feed": self.dataset.get("feed")}

    def export_run(self, directory: Path | None = None) -> Path:
        from .reporting import export_run
        with self.lock:
            return export_run(self, directory or Path(self.config.runs_dir))

    def close(self) -> None:
        with self.lock:
            self.finish("environment closed")
            self.closed = True


class ReplayGymEnv(gym.Env):
    """Fixed symbol order; integer share changes submitted in that order."""
    metadata = {"render_modes": []}

    def __init__(self, bars: list[Bar], config: Config | None = None, dataset: dict | None = None,
                 history_length: int = 8, reward_fn: Callable[[dict, dict], float] | None = None):
        self.config = config or Config()
        self.core = MarketEnvironment(bars, self.config, dataset=dataset)
        self.history_length, self.reward_fn = history_length, reward_fn
        if history_length < 1 or len(self.core._groups) < 2:
            raise ValueError("Gym replay needs history_length >=1 and at least two event times")
        n, limit = len(self.config.symbols), self.config.max_order_shares
        self.action_space = spaces.Box(-limit, limit, (n,), dtype=np.int64)
        self.observation_space = spaces.Dict({
            "history": spaces.Box(0, np.inf, (history_length, n, 5), dtype=np.float64),
            "mask": spaces.MultiBinary((history_length, n)),
            "volume_mask": spaces.MultiBinary((history_length, n)),
            "cash": spaces.Box(0, np.inf, (1,), dtype=np.float64),
            "holdings": spaces.Box(0, np.iinfo(np.int64).max, (n,), dtype=np.int64)})

    def _observation(self):
        history = np.zeros((self.history_length, len(self.config.symbols), 5), dtype=np.float64)
        mask = np.zeros(history.shape[:2], dtype=np.int8)
        volume_mask = np.zeros(history.shape[:2], dtype=np.int8)
        # Time grid includes missing symbol bars; no forward-filled OHLCV.
        times = sorted({b.end for values in self.core.price_history.values() for b in values})[-self.history_length:]
        offset = self.history_length - len(times)
        for j, symbol in enumerate(self.config.symbols):
            lookup = {b.end: b for b in self.core.price_history[symbol]}
            for i, at in enumerate(times, offset):
                bar = lookup.get(at)
                if bar:
                    history[i, j] = [float(getattr(bar, k)) if getattr(bar, k).is_finite() else 0.
                                     for k in ("open", "high", "low", "close", "volume")]
                    mask[i, j] = 1
                    volume_mask[i, j] = int(bar.volume.is_finite())
        portfolio = self.core.get_portfolio()
        return {"history": history, "mask": mask, "volume_mask": volume_mask,
                "cash": np.array([float(portfolio["cash"])], dtype=np.float64),
                "holdings": np.array([portfolio["holdings"][s] for s in self.config.symbols], dtype=np.int64)}

    def _info(self):
        # Wall-clock observations are available from core.diagnostics(); keeping
        # Gym info market-only makes seeded historical replay deterministic.
        return {"market_time": str(self.core.market_time), "symbols": self.config.symbols,
                "feed": self.core.dataset.get("feed"), "ended": self.core.ended}

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        self.core.reset()
        return self._observation(), self._info()

    def step(self, action):
        action = np.asarray(action)
        if action.dtype.kind not in "iu" or not self.action_space.contains(action):
            raise ValueError("Action must be an integer vector within action_space")
        if self.core.ended:
            raise RuntimeError("Episode ended; reset required")
        before = self.core.get_portfolio()
        for symbol, quantity in zip(self.config.symbols, action):
            if quantity:
                self.core.submit_order(symbol, "buy" if quantity > 0 else "sell", abs(int(quantity)))
        self.core.advance_replay()
        after = self.core.get_portfolio()
        reward = self.reward_fn(before, after) if self.reward_fn else float(after["equity"] - before["equity"])
        return self._observation(), float(reward), self.core.ended, False, self._info()

    def close(self):
        self.core.close()
