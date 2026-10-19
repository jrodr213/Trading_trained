# Local market experimentation environment

This installable Python package downloads **consolidated SIP historical bars**, replays them offline, and polls newly available delayed SIP bars. It provides a manual console, a typed Python API, and a Gymnasium historical interface. All accounts, orders, fills, and money are local. Starting it creates **no orders**.

There are no agents, policies, signals, learning algorithms, model downloads, brokerage connections, external order submission, dashboard, or cloud deployment. The existing repository license is preserved. The implementation is original and does not use FinRL.

## Setup

Run from this `testing` folder. Python 3.11+ is supported; the checked installation uses Python 3.12.10. The supplied lock file records the exact packages installed during verification. It is a version lock, not a cryptographic wheel/hash lock. Platform-specific wheels are resolved by pip.

PowerShell:

```powershell
cd testing  # omit if already here
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --cache-dir .cache/pip -r requirements.lock
python -m pip install --no-build-isolation --no-deps -e .
python -m pytest -q
python -m market_env demo
```

If PowerShell blocks activation, use `.\.venv\Scripts\python.exe` in place of `python`; no execution-policy change is needed.

Bash:

```bash
cd testing  # omit if already here
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --cache-dir .cache/pip -r requirements.lock
python -m pip install --no-build-isolation --no-deps -e .
python -m pytest -q
python -m market_env demo
```

For dependency updates, install `python -m pip install --cache-dir .cache/pip -e '.[test]'`, verify the suite, then intentionally regenerate the lock. `alpaca-py==0.44.0` is pinned because per-request timeout/budget integration uses its internal session and disables its built-in retries. No prohibited training packages are installed. Pytest's HTTP guard fails unmocked network calls; its caches and temporary files stay under `.cache/` in this project.

## Credentials and configuration

Demo, replay, and tests work without credentials. Download and follow require market-data credentials. Create `.env` **only if it does not exist**:

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

```bash
test -e .env || cp .env.example .env
```

Fill in `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` yourself. Environment variables take precedence. The loader reads only this project's `.env`, never parent directories. Credentials are never exported; `.env` is ignored by Git. Missing credentials produce a clean configuration error.

`config.toml` controls symbol order, interval, intentional delay, polling, retries, budget, costs, account limits, staleness, and output paths. All CLI paths resolve relative to the project root and must stay inside it. Run elsewhere using `python -m market_env --project /path/to/testing ...`. Supported intervals are 1, 5, and 15 minutes, regular NYSE sessions only. Default symbol order is **KO, JPM, IBM**, never alphabetically reordered. Overlap is at least one configured interval. Monetary configuration values are decimal strings.

## Commands

```text
python -m market_env demo
python -m market_env download --symbols KO JPM IBM --start 2026-09-01T00:00:00Z --end 2026-09-05T00:00:00Z --output data/history.csv
python -m market_env replay --data data/history.csv
python -m market_env replay --data data/history.csv --session 2026-09-02
python -m market_env follow --symbols KO JPM IBM
python -m pytest -q
```

Download also accepts `--interval 5` and `--refresh`. Timestamps must have explicit timezone offsets and are normalized to UTC. Invalid/empty ranges are rejected before sending a request. Downloads contain CSV plus `.csv.json` metadata: raw adjustment, provider/feed, retrieval time, ordered symbols, interval, requested/effective ranges, coverage, schema version, and SHA-256 identity. Each row contains symbol, bar start/end, OHLC, and volume. Existing identical validated downloads are cached; a different identity requires another filename or `--refresh`. A still-moving cutoff changes the effective range and therefore the cache identity. Missing symbols and expected regular-session gaps are reported individually; gaps can also be genuine periods without trades. No prices are invented.

`demo` creates the tiny, deterministic **SYNTHETIC** dataset under `data/SYNTHETIC.csv` and opens replay. It is invented data, unsuitable for performance claims. The tracked copy lives in `tests/fixtures/SYNTHETIC.csv` with its sidecar. Demo generation uses its fixed fixture symbols regardless of configured download symbols.

The console supports:

```text
prices
status
portfolio
buy KO 5
orders
cancel o000001
buy KO 2
next
sell KO 1
next
export
quit
```

These are manual syntax examples, not a bundled strategy. `next` advances replay only. Follow acquisition and hypothetical fills run on a background thread while input waits; an `RLock` protects observations, order submission, execution, and export. Use `status` to inspect connection failures or delays. Ctrl+C, EOF, and quit cancel pending orders and export the run. Export can also be requested at any time. No session is liquidated automatically.

## Delayed SIP timeline

Follow is labeled **DELAYED SIP DATA / LOCAL SIMULATED TRADES**. It polls historical bars; it does not use latest/snapshot endpoints, streaming, scraping, IEX fallback, or a brokerage calendar. [Alpaca's market-data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq) describes consolidated SIP coverage and the historical recent-data restriction. SIP covers trades across reporting US venues for the selected symbols; it is neither an NYSE-only direct feed nor a full order book.

Every request specifies `feed=DataFeed.SIP`, raw adjustment, and an explicit UTC end:

```text
safe_cutoff = wall_time - delay_minutes - safety_buffer_seconds
request_end = min(requested_end, safe_cutoff)
eligible bar: start >= requested_start AND end <= request_end
```

Defaults are 15 minutes of delay, 30 seconds of safety buffer, and 30-second polling. A bar timestamp marks its **start**: a 13:31 minute bar remains hidden until its 13:32 end is eligible. Both the request and the full-bar filter enforce the cutoff. Shortened download ranges are printed. An intentional delay does not itself indicate unexpected staleness.

`status` reports wall time, observed market time, expected completed delayed bar, per-symbol latest bar, actual age, intentional delay (including buffer), unexpected lag, connection, and both wall/market session status. Expected time uses the NYSE calendar, including holidays, early closes, DST, and weekends; it stops advancing at session close. Simulated order acceptance uses the **market** session clock. Thus closing bars continue arriving after the wall-clock exchange has closed. If launched before a session or on a holiday/weekend, follow waits for the next eligible regular session. A session started after its delayed close simply replays that day's eligible bars and completes.

Follow tracks successful request-through times separately per symbol, fetches with overlap, consumes every SDK page, and deduplicates `(symbol, start)`. Genuine old gaps remain recorded and are retried in bounded 10-minute windows, one per symbol per poll, with rotation so a persistent gap cannot starve others. Recoverable outages preserve cursors and buffers; the failed symbol resumes from its previous cursor. A partial HTTP pagination failure never commits that symbol's range.

Bars wait for other symbols at the same event time, up to the configured unexpected-lag tolerance (default 120 seconds behind the eligible cutoff). After this grace period, missing symbols remain masked and later event times can advance. A bar arriving **after its event time was exposed** is logged but never inserted into historical observations or used for retroactive fills. Vendor revisions to exposed bars are also logged separately and ignored. Corrections received before exposure can update the private buffer. A follow process restart starts a new account; there is no persistent account resume.

Every HTTP attempt, including SDK pagination and retries, passes through a sliding-window budget (default 120/minute, configurable up to 180). Requests have a 15-second timeout per connect/read operation. HTTP 429, selected transient 5xx errors, timeouts, and connection errors have three bounded retries with capped exponential backoff. Each poll can recover later after exhausted transient retries; new orders are rejected while paused. Authentication/entitlement errors stop the session and retain the actual server error with known credentials redacted. No fallback provider is selected. Shutdown interrupts budget/backoff waits and waits for bounded in-flight HTTP work.

Pagination was verified by reading installed `StockHistoricalDataClient.get_stock_bars` and `RESTClient._get_marketdata` in alpaca-py 0.44.0. The latter follows `next_page_token` until absent and aggregates symbols. The request intentionally omits a total limit; the SDK uses 10,000-item pages. Offline tests exercise the **real installed SDK** over mocked HTTP with symbols split across pages, and assert UTC fields, feed, page tokens, timeouts, and per-attempt budgets. See the official [historical SDK](https://alpaca.markets/sdks/python/api_reference/data/stock/historical.html), [request models](https://alpaca.markets/sdks/python/api_reference/data/stock/requests.html), and [bars endpoint](https://docs.alpaca.markets/us/reference/stockbars).

## Local accounting and limitations

The shared simulator defaults to $100,000, whole shares, long-only, no margin, and market orders. Fees and slippage are configurable. Monetary cashflows, execution prices, fees, equity, and allocated cost basis round to cents using decimal `ROUND_HALF_UP`. Average cost is remaining book cost divided by remaining shares; buy fees enter cost basis and sell fees reduce realized proceeds. A final sale consumes the entire remaining book cost to avoid rounding residue. Reported performance is **price-only P/L, not dividend-inclusive total return**.

Buy reservations use the latest observed close with slippage plus fees. Pending sells reserve shares; pending buys reserve cash. Canceling releases reservations. Execution rechecks affordability while preserving other orders' reservations. An unaffordable opening gap rejects the **entire order**, without partial fills or negative cash. Client IDs are idempotent for identical requests; reusing one for a different request raises `ValueError`. Invalid symbol/side/quantity, size excess, overspending, overselling, missing/stale data, closed sessions, and ended environments produce explicit rejections. Rejected requests are retained in order/event history.

An order submitted after a completed bar fills no earlier than the next received eligible bar whose start is at or after the submission market time. The fill uses that bar's open, with slippage; fills are processed **before** its remaining OHLCV is exposed. Orders for a missing symbol wait for its next actual bar. Marks can remain old for valuation and are flagged missing in observations. Event times process chronologically; within an event, symbols process in configured order, then orders FIFO within each symbol. Available sell proceeds cannot fund an earlier processed buy. There is no volume cap, bid/ask book, liquidity model, partial fill, queue priority, halt feed, or exchange execution claim. Both market event time and time the hypothetical fill became known are recorded. Delayed fills are **past-market hypothetical trades**, not orders executable at today's prices.

**Corporate-action boundary:** no account carries positions across sessions. Datasets may contain multiple sessions; the CLI visibly selects the first unless `--session` is provided. The Python core rejects multi-session replay input. Follow completes after its delayed session and requires a new process/account for another session. Pending orders are canceled at the boundary. Only raw regular-session prices are accepted; adjusted/raw mixing is rejected. Splits, dividends, mergers, symbol changes, and unusual intraday corporate actions are not modeled. This limitation avoids carrying raw share counts over ordinary overnight splits; do not use affected intraday sessions or interpret independently reset sessions as one portfolio. Corporate-action completeness has not been authenticated or verified against a provider. No automatic liquidation or invented terminal prices occurs.

## Python API and Gymnasium

```python
from pathlib import Path
from dataclasses import replace
from market_env import Config, MarketEnvironment, ReplayGymEnv
from market_env.data import load_dataset

bars, identity = load_dataset(Path("data/SYNTHETIC.csv"))  # SYNTHETIC
config = replace(Config(), symbols=tuple(identity["symbols"]),
                 interval_minutes=identity["interval_minutes"])
env = MarketEnvironment(bars, config, dataset=identity)
observation = env.get_observation()
portfolio = env.get_portfolio()
env.export_run(Path("runs"))
env.close()
```

Future external decision code can connect to the following API; no Agent class or policy is supplied:

| Method | Behavior |
| --- | --- |
| `reset() -> dict` | New replay account; exposes only the first event time. A running follow instance requires a new environment instead. |
| `get_observation() -> dict` | Ordered symbols, completed history of immutable `Bar` objects, market timestamp, missing masks, portfolio, feed label. |
| `submit_order(symbol: str, side: str, quantity: int, client_order_id: str \| None = None) -> dict` | Local pending/rejected order; never sends an external order. |
| `cancel_order(order_id: str) -> bool` | Cancels only pending orders. |
| `get_portfolio() -> dict` | Cash/available cash, holdings/available shares, cost basis, realized/unrealized P/L, fees, slippage, equity. |
| `advance_replay() -> bool` | Advances one chronological event group, or false at end; unavailable in follow. |
| `diagnostics() -> dict` | Both clocks, expected delay/lag, session and connection status. |
| `export_run(directory: Path \| None = None) -> Path` | Creates a unique experiment directory. |
| `close()` | Cancels pending orders and ends the environment. |

`ReplayGymEnv(bars, config, dataset=identity, history_length=8, reward_fn=None)` implements the [Gymnasium API](https://gymnasium.farama.org/api/env/) and passes `gymnasium.utils.env_checker.check_env`.

- `reset(seed=None, options=None)` returns `(observation, info)`; `step(action)` returns `(observation, reward, terminated, truncated, info)`.
- The action space is an integer `Box` vector in fixed symbol order, bounded by `max_order_shares`. Positive shares buy, negative shares sell, zero does nothing. Noninteger/out-of-bounds vectors raise `ValueError`; each valid nonzero request passes through simulator validation. Simultaneous requests process **KO then JPM then IBM** by default, using existing reservations; later requests see earlier reservations.
- The observation space is a `Dict`: `history` is `(history_length, symbols, 5)` float64 OHLCV, `mask` is a matching binary time/symbol mask, `cash` is length 1 float64, and `holdings` is an integer vector. Padding and absent bars contain zeros with mask 0, never synthetic replacement prices. History uses observed event times, not future data or forward-filled OHLCV. Account calculations remain Decimal internally.
- `info` contains only market time, symbol order, feed, and episode end status. Wall-time diagnostics are separately available on `gym_env.core.diagnostics()` so Gym replay remains deterministic. Seeds are accepted; there is no runtime randomness.
- Default reward is after-equity minus before-equity, already net of fees/slippage; fees are not subtracted twice. Replace it with `reward_fn(before_portfolio, after_portfolio) -> float`.
- The last real event returns `terminated=True`, `truncated=False`; pending orders are canceled and holdings retain the last observed marks. Further steps require reset. No liquidation occurs. Gym requires at least two event times.

**Future agents must not combine delayed/historical prices with current news, current fundamentals, revised future data, or any information unavailable at the simulated time and then claim an unbiased historical experiment.** Respect the market clock for every external input. A local environment interface cannot enforce the timing of an external data source.

## Outputs and verification

Each export creates `runs/<UTC timestamp>-<UUID>/`, never overwriting another experiment. It contains configuration, source and observed dataset identities, observed CSV/sidecar, both clocks/status, orders, fills, rejections/cancellations, fees/slippage, portfolio history, acquisition diagnostics, ignored revisions/late arrivals, and package/Python versions. Only exposed bars enter `observed.csv`; exports do not reveal remaining replay data. The acquisition log reports fetched range/coverage and failures, not HTTP headers or secrets. Generated data, runs, caches, local environment, and `.env` are ignored by Git.

The offline suite covers cutoff/full intervals/UTC, actual SDK pagination over mock HTTP, retry/rate limits and errors, validation/cache identity, NYSE schedules, delay/staleness, reconnect/backfill/dedup/revisions, delayed close, accounting/reservations/gaps, no look-ahead, deterministic rewards/boundaries, Gym validation, console commands, and absence of brokerage/training code. Gym may warn about integer share actions not being normalized and unbounded observation maxima; those spaces intentionally represent shares and prices. Alpaca currently imports a deprecated websockets compatibility module despite this application using HTTP only.

Authenticated Alpaca access and live entitlement, real SIP coverage, real server outages, and long-running polling **were not tested**. Mocked tests verify local behavior only and do not verify network access. The dependencies were actually installed into `.venv`; offline tests and CLI smoke checks were run locally. This is a local experimentation tool with the explicit session/corporate-action and bar-execution limits above.
