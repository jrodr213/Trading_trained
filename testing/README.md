# Local market experimentation environment

**Yahoo OTC extension:** `--provider yahoo` adds credential-free Yahoo/yfinance downloads and delayed OTC polling to the same replay, Gymnasium, and local accounting interfaces. Alpaca remains the default. The original Alpaca/SYNTHETIC instructions and single-session restrictions below remain unchanged for those datasets; the final **Yahoo Finance / OTC extension** section describes Yahoo's additional intervals, multi-period replay, and accounting assumptions.

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

## Yahoo Finance / OTC extension

The optional selection `--provider yahoo` uses **yfinance 1.7.0** in the existing package. It shares `Bar`, `MarketEnvironment`, `ReplayGymEnv`, `Simulator`, the console, and experiment exports with Alpaca. `providers.MarketDataProvider` documents the common `fetch(symbols, start, end) -> (bars, effective_end)` boundary; `make_provider(config, root)` selects it. Omitting `--provider` still selects Alpaca through the unchanged default configuration. Existing CSV datasets and their sidecars remain readable, and saved Alpaca data/runs have not been moved or rewritten. No AI agents or brokerage methods were added.

The installed dependencies are recorded in `requirements.lock`; update an existing installation from this folder with:

```powershell
.\.venv\Scripts\python -m pip install --cache-dir .cache/pip -r requirements.lock
.\.venv\Scripts\python -m pip install --no-build-isolation --no-deps -e .
```

Bash uses `.venv/bin/python` for the same commands. The additions are yfinance and pyarrow (Parquet), plus their dependencies; the original dependencies remain installed. Yahoo needs no Alpaca credentials. Its timezone/cookie cache stays in `.cache/yfinance` inside this project and is ignored by Git.

### Download, coverage, replay, and follow

Run these commands from `testing` with the virtual environment activated:

```text
python -m market_env download --provider yahoo --symbols NTDOY NSRGY RHHBY --start 2020-01-01 --end 2025-01-01 --interval 1d --output data/yahoo/otc/daily
python -m market_env coverage --data data/yahoo/otc/daily/NTDOY.csv
python -m market_env replay --data data/yahoo/otc/daily/NTDOY.csv
python -m market_env replay --data data/yahoo/otc/daily/NTDOY.parquet
python -m market_env follow --provider yahoo --symbols NTDOY NSRGY
python -m market_env follow --provider yahoo --symbols NTDOY --interval 5m --poll-interval 180
python -m pytest -q
```

NTDOY, NSRGY, and RHHBY are examples, not a ticker allowlist or a complete OTC universe. Any requested ticker is passed to Yahoo; missing/delisted/unsupported tickers get individual diagnostics while valid tickers continue. Exchange metadata determines `is_otc` as true, false, or unknown; a Yahoo ticker is never automatically declared OTC. Classification describes metadata at retrieval time, not a historical security master. Foreign-currency securities can be downloaded, but the shared USD simulation rejects non-USD or unknown currency data instead of silently mixing currencies. Use one exchange calendar/timezone per account; multi-ticker downloads produce separate datasets.

Directory output defaults to **both** `NTDOY.csv` and `NTDOY.parquet`, each with its own `.json` sidecar and SHA-256. Use `--format csv` or `--format parquet` to select one format. A single ticker can also target an explicit file, e.g. `--output data/yahoo/otc/NTDOY.csv`. Parquet stores decimal fields as compressed strings to preserve their exact precision. The CSV format has the same symbol/start/end/OHLCV columns as Alpaca; provider, interval, retrieval time, original ticker, available range, adjustment, actions, and diagnostics live in the sidecar.

Received source frames (including Adj Close and available dividend/split columns) are retained under the output folder's `sources/` with unique filenames. They are not overwritten when transformed. The metadata records their path and hash. Each download writes a separate `download-report-<UUID>.json`, including per-symbol failures. Identical validated request/range/interval/provider identities use the local cache. Changing the request under the same filename requires `--refresh` or another output directory. Refresh replaces normalized outputs intentionally and retains earlier source files. Nothing fetches data merely to replay an existing local dataset.

Daily `1d`, weekly `1wk`, and monthly `1mo` history have **no five-year application cap**: explicit start/end dates are sent to Yahoo. Supported intraday intervals are `1m`, `2m`, `5m`, `15m`, `30m`, `60m`, `90m`, and `1h`. Conservative request bounds are 7 days for 1m, 730 days for 60m/1h, and 60 days for other intraday intervals; these are ceilings, not promises of availability. Yahoo can impose shorter windows or return no OTC intraday history. The application reports the actual available range and server error; it never silently switches to daily data. Try `--interval 1d` for unavailable older intraday history. Date-only inputs mean midnight UTC; timestamp inputs require an explicit offset.

All returned bars are validated for prices, OHLC, bounds, and volume. Invalid rows are reported and excluded, duplicate timestamps retain the first valid row, and missing rows are never replaced with invented prices. A missing volume remains NaN in the native bar with a warning. Gym observations put 0 in that numeric slot **with `volume_mask=0`**, separate from the missing-bar `mask`; the zero is padding, not a claim of zero traded volume. The additional `volume_mask` is present for both providers. A reported zero-volume bar is distinguished from unknown volume and failed requests.

### Completed periods and delayed OTC polling

Yahoo follow is labeled **DELAYED OTC MARKET / LOCAL SIMULATED TRADES**, defaults to 5-minute historical bars and 120-second polling, and keeps polling across session breaks until quit/Ctrl+C or an explicit share-basis stop. It never places an order on startup. The existing console commands work unchanged; `next` is replay-only. Polling follows incremental per-symbol request-through cursors with overlap; failed symbols retain their earlier cursor for recovery. Previously exposed revisions/late bars are logged without rewriting observations or fills.

[Yahoo's coverage table](https://help.yahoo.com/kb/finance/SLN2310.html) lists OTC Markets Group data with a 15-minute delay. The 15-minute expectation is not a guarantee that each security updates on that schedule. Follow filters historical bars against the existing injected wall-clock cutoff (15 minutes plus the default 30-second buffer). A full interval must be completed and eligible. Daily bars become visible after their session end, weekly bars after the last reference session of that week, and monthly bars after the month's final reference session. Current incomplete aggregates are hidden. This prevents using a whole day's OHLCV at its opening label.

`status` shows the latest completed price/bar timestamp, wall time, actual observation age, provider, expected delay, additional lag, request status/error, and volume/activity warnings per symbol. The timestamp is a **completed-bar event proxy**, not an exact last-trade timestamp; Yahoo OHLCV does not establish the precise last trade. Reported zero volume indicates an inactive interval. An absent or older bar cannot by itself distinguish a legitimately inactive security from missing/delayed vendor data, so status explicitly marks that uncertainty instead of declaring an outage or inventing a trade. A failed/rate-limited request has its own status and blocks new orders for that symbol. Intentional delay alone does not make data unexpectedly stale.

Known exchange metadata selects an exchange calendar where available. OTC metadata selects an explicitly labeled **NASDAQ US equity reference**, since the installed calendar library has no authoritative OTC venue calendar. It supplies reference holidays/early closes/DST for expected-delay diagnostics and period timestamps, **not a guarantee that OTC activity matches NYSE/Nasdaq hours**. Supplied OTC bars are not rejected merely because this reference marks their date closed; conservative civil-period bounds are used then. Unknown venues use conservative civil periods and preserve timezone/unknown classification. Extended/overnight OTC execution and a venue-specific OTC session calendar are not modeled. No missing OTC bar is treated as proof that a trade should have occurred.

The default Yahoo budget is 10 **history operations** per minute, not a claimed count of yfinance's internal HTTP requests. yfinance may make additional ordinary cookie/timezone/chart requests. Per-history timeouts are supplied explicitly. Errors, including HTTP 429, cause a configurable 120-second backoff for that symbol; repeated polls during backoff make no new history call. There are no custom access workarounds, proxy rotation, scraping bypasses, or rapid retry loops. Shutdown interrupts local budget/backoff waits; an in-flight yfinance request must return or reach its timeout.

### Accounting basis and estimated fills

Every request explicitly specifies `auto_adjust=False`, `back_adjust=False`, `actions=True`, `repair=False`, and `prepost=False`. **Adj Close is archived but never used for fills or marks.** Yahoo OHLC can already be split-normalized even with auto adjustment off; the sidecar names the basis `yahoo_split_normalized_no_dividend_adjustment`. Historical Yahoo replay therefore uses fixed whole **Yahoo-normalized share units**, not a claim about contemporaneous actual share quantities or executable historical prices. A normalized unit can correspond to a fractional pre-split actual share. Supplied split events are recorded without multiplying holdings a second time. Dividends are recorded without crediting cash; performance is price-only P/L, never dividend-inclusive total return. Weekly/monthly action values may be aggregated at the provider's period label rather than an exact action date.

Yahoo multi-period replay supports overnight holdings **on this fixed normalized basis**; Alpaca retains its original raw-price single-session restriction. Yahoo follow stops and cancels pending orders if a newly reported split may change an already-running account's basis. It does not rescale old marks, shares, or fills silently; start a fresh account after checking the new data. Incomplete corporate-action data, ADR ratio changes, mergers, ticker changes, and unreported actions remain research limitations. The original untransformed source is retained for inspection.

The shared simulator still starts with $100,000 and uses whole units, reservations, idempotent orders, cost basis, fees, slippage, P/L, and audit exports. Yahoo defaults to six decimal places for execution prices (`yahoo_execution_price_tick`), while cashflows/equity still round to cents. Orders whose total notional rounds to zero are rejected. Existing Alpaca execution precision stays at cents. Configure `fee_per_order`, `fee_per_share`, and `slippage_bps` in the same `config.toml`; defaults remain unchanged.

Yahoo orders wait until a later eligible bar's open, after acceptance, and never reuse an already-observed open. Daily/weekly/monthly closing observations can accept requests queued for the next period's open. To avoid claiming instantaneous liquidity, Yahoo also limits each symbol's fills to a configurable fraction of **previously observed** volume (`yahoo_volume_participation`, default 10%); insufficient capacity leaves the whole order pending. Unknown/zero previous volume offers no capacity. Capacity does not inspect the execution bar's future OHLCV. Fills use the same configurable costs and slippage, carry an **ESTIMATED OHLCV FILL** label, and warn on low prior volume. This is not a queue/impact model or proof of available opening liquidity. No bid/ask quotes are invented.

Other new configuration keys are `provider`, `yahoo_interval`, `yahoo_poll_interval_seconds`, `yahoo_requests_per_minute`, `yahoo_backoff_seconds`, and `low_volume_threshold`. Default provider and Alpaca request settings are unchanged. Future external decision code uses exactly the existing observation/order/replay methods; pass loaded Yahoo bars and their sidecar identity to `MarketEnvironment` or `ReplayGymEnv`, with matching symbol order. Continue to obey the earlier prohibition on combining historical/delayed observations with future news or other unavailable information.

### Usage rights and verification

[yfinance is an independent, unofficial project](https://github.com/ranaroussi/yfinance), not an endorsed Yahoo SDK. Respect Yahoo's applicable terms and data usage rights; yfinance describes personal research/educational usage. OTC coverage and delisted history are incomplete, intraday data can be unavailable, security prices can be old, and Yahoo data is not guaranteed for execution-quality simulation. The integration remains local research infrastructure with no real-money or brokerage path.

On October 20, 2026, a read-only live request for **NTDOY daily history, January 2–8, 2025 (end exclusive)** succeeded, returned four completed daily bars through January 7, and supplied PNK/OTC Markets metadata with USD currency. Data and original responses are retained in ignored `data/yahoo/otc/smoke-20261020/`. Offline replay of that downloaded file, manual local order submission, valuation, and export were also exercised. This verifies that small historical request only. Live OTC intraday availability, sustained polling, broad symbol coverage, and real rate-limit recovery remain unverified; their behavior is covered by offline mocks. Authenticated Alpaca access has still not been tested.
