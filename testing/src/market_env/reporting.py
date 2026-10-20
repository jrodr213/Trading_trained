from datetime import datetime
from decimal import Decimal
from pathlib import Path
from importlib.metadata import version
import json
import platform
import uuid
from dataclasses import asdict
from .data import write_dataset, metadata


def encode(value):
    if isinstance(value, (datetime, Decimal, Path)):
        return str(value)
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(type(value).__name__)


def export_run(env, directory):
    path = Path(directory) / (env.clock.now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex)
    path.mkdir(parents=True, exist_ok=False)
    observed = sorted((bar for bars in env.price_history.values() for bar in bars), key=lambda b: (b.end, env.config.symbols.index(b.symbol)))
    identity = dict(env.dataset)
    if observed:
        if env.yahoo:
            from .yahoo import yahoo_coverage
            meta = {**env.dataset, "symbols": list(env.config.symbols), "retrieved_at": env.clock.now().isoformat(),
                    "requested_start": observed[0].start.isoformat(), "requested_end": observed[-1].end.isoformat(),
                    "effective_start": observed[0].start.isoformat(), "effective_end": observed[-1].end.isoformat(),
                    "coverage": yahoo_coverage(observed, env.config.symbols, observed[0].start, observed[-1].end,
                                               env.config.yahoo_interval, env.dataset.get("securities", {}))}
        else:
            meta = metadata(env.config, observed, observed[0].start, observed[-1].end, observed[-1].end,
                            env.clock, synthetic=env.dataset.get("feed") == "SYNTHETIC")
        identity["observed_dataset"] = write_dataset(path / "observed.csv", observed, meta)
    values = {"configuration": env.config.to_dict(), "dataset": identity,
              "clocks_and_status": env.diagnostics(), "portfolio": env.get_portfolio(),
              "orders": env.get_orders(), "fills": env.sim.fills, "events": env.sim.events,
              "portfolio_history": env.sim.history, "revisions_and_late_bars": env.revisions,
              "acquisition": env.acquisition,
              "versions": {"python": platform.python_version(), **{name: version(name) for name in
                           ("local-market-env", "alpaca-py", "gymnasium", "pandas-market-calendars", "numpy", "yfinance", "pyarrow")}},
              "limitations": (["Yahoo estimated bar fills; fixed provider split-normalized share units", "No dividend credit; price-only P/L",
                                "OTC reference calendar does not certify activity; no FX conversion"] if env.yahoo else
                               ["Local hypothetical bar fills", "Single NYSE regular session; raw prices",
                                "Price-only P/L; no dividends, corporate actions, or overnight holdings"])}
    for name, value in values.items():
        (path / f"{name}.json").write_text(json.dumps(value, indent=2, default=encode), encoding="utf-8")
    return path
