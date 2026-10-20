import argparse
from dataclasses import replace
from pathlib import Path
import sys
from .config import Config
from .clock import utc, NYSE
from .data import AlpacaProvider, DataError, download, load_dataset
from .environment import MarketEnvironment
from .follow import Follower
from .console import console, show
from .replay import create_synthetic
from .providers import make_provider


def main(argv=None):
    parser = argparse.ArgumentParser(description="Historical/delayed Alpaca SIP or Yahoo data and LOCAL simulated trades only")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Project root containing config.toml and .env")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="SYNTHETIC offline replay; no credentials or orders")
    dl = commands.add_parser("download")
    dl.add_argument("--provider", choices=["alpaca", "yahoo"])
    dl.add_argument("--symbols", nargs="+")
    dl.add_argument("--start", required=True)
    dl.add_argument("--end", required=True)
    dl.add_argument("--output", type=Path)
    dl.add_argument("--interval", help="Alpaca: 1,5,15 minutes; Yahoo: 1d,1wk,1mo or supported intraday interval")
    dl.add_argument("--format", choices=["csv", "parquet", "both"], default="both", help="Yahoo directory output formats")
    dl.add_argument("--refresh", action="store_true")
    rp = commands.add_parser("replay")
    rp.add_argument("--data", type=Path, required=True)
    rp.add_argument("--session", help="Select one NYSE session, YYYY-MM-DD")
    fw = commands.add_parser("follow")
    fw.add_argument("--provider", choices=["alpaca", "yahoo"])
    fw.add_argument("--symbols", nargs="+")
    fw.add_argument("--interval")
    fw.add_argument("--poll-interval", type=float, help="Polling seconds (Yahoo default 120)")
    inspect = commands.add_parser("coverage", help="Inspect local CSV/Parquet coverage without network requests")
    inspect.add_argument("--data", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.project.resolve()
    provider = None
    try:
        config = Config.load(root / "config.toml")
        if getattr(args, "provider", None):
            config = replace(config, provider=args.provider)
        # All configured generated paths must remain under this project.
        def local(path):
            result = (root / path).resolve()
            if not result.is_relative_to(root):
                raise ValueError("Paths must stay inside the project folder")
            return result
        config = replace(config, data_dir=str(local(config.data_dir)), runs_dir=str(local(config.runs_dir)))
        if getattr(args, "symbols", None):
            config = replace(config, symbols=tuple(s.upper() for s in args.symbols))
        if getattr(args, "interval", None):
            if config.provider == "yahoo":
                config = replace(config, yahoo_interval=args.interval)
            else:
                config = replace(config, interval_minutes=int(args.interval))
        if getattr(args, "poll_interval", None) is not None:
            config = replace(config, **{("yahoo_poll_interval_seconds" if config.provider == "yahoo" else "poll_interval_seconds"): args.poll_interval})
        if args.command == "demo":
            print("SYNTHETIC fixture: deterministic invented prices, not market data")
            bars, meta = create_synthetic(Path(config.data_dir) / "SYNTHETIC.csv")
            config = replace(config, symbols=tuple(meta["symbols"]), interval_minutes=1)
            console(MarketEnvironment(bars, config, dataset=meta))
        elif args.command == "replay":
            bars, meta = load_dataset(local(args.data))
            if meta["provider"] == "Yahoo":
                from .yahoo import dataset_calendar
                calendar = dataset_calendar(meta)
                if args.session:
                    bars = [b for b in bars if b.start.astimezone(calendar.timezone).date().isoformat() == args.session]
                    meta = {**meta, "selected_session": args.session}
                config = replace(config, symbols=tuple(meta["symbols"]), provider="yahoo", yahoo_interval=meta["interval"])
            else:
                calendar = NYSE()
                sessions = sorted({calendar.session_for(b.start, b.end) for b in bars})
                session = args.session or sessions[0]
                if len(sessions) > 1 and not args.session:
                    print(f"Single-session raw-price account: selecting {session}. Use --session YYYY-MM-DD for another session.")
                bars = [b for b in bars if calendar.session_for(b.start, b.end) == session]
                meta = {**meta, "selected_session": session}
                config = replace(config, symbols=tuple(meta["symbols"]), provider="alpaca", interval_minutes=meta["interval_minutes"])
            if not bars:
                raise DataError("No bars in selected session")
            show(meta["coverage"])
            console(MarketEnvironment(bars, config, dataset=meta))
        elif args.command == "download":
            if config.provider == "yahoo":
                from .yahoo import yahoo_time, download_yahoo
                start, end = yahoo_time(args.start), yahoo_time(args.end)
                if start >= end:
                    raise DataError("start must precede end")
                provider = make_provider(config, root)
                category = "intraday" if config.yahoo_interval.endswith(("m", "h")) and config.yahoo_interval != "1mo" else "daily"
                output = local(args.output or Path(config.data_dir) / "yahoo" / "otc" / category)
                result = download_yahoo(provider, config, start, end, output, args.refresh, args.format)
                show(result)
                if not any(r["status"] in ("cached", "downloaded") for r in result.values()):
                    raise DataError("No requested Yahoo tickers produced completed valid bars; see per-symbol diagnostics")
                return 0
            # Parse/validate dates before credentials or any network call.
            start, end = utc(args.start), utc(args.end)
            if start >= end:
                raise DataError("start must precede end")
            provider = make_provider(config, root)
            output = local(args.output or Path(config.data_dir) / "history.csv")
            _, meta = download(provider, config, start, end, output, args.refresh)
            show({"output": str(output), "coverage": meta["coverage"],
                  "effective_start": meta["effective_start"], "effective_end": meta["effective_end"]})
        elif args.command == "coverage":
            _, meta = load_dataset(local(args.data))
            show(meta)
        else:
            if config.provider == "yahoo":
                from .yahoo import INTERVALS, ADJUSTMENT
                from .yahoo_follow import YahooFollower
                if not getattr(args, "interval", None) and not INTERVALS[config.yahoo_interval]:
                    config = replace(config, yahoo_interval="5m")
                provider = make_provider(config, root)
                identity = {"version": 2, "provider": "Yahoo", "feed": "yahoo", "interval": config.yahoo_interval,
                            "interval_minutes": INTERVALS[config.yahoo_interval], "adjustment": ADJUSTMENT,
                            "symbols": list(config.symbols), "securities": {}, "corporate_actions": {}}
                env = MarketEnvironment([], config, dataset=identity, follow=True)
                follower = YahooFollower(env, provider)
            else:
                provider = make_provider(config, root)
                env = MarketEnvironment([], config, dataset={"provider": "Alpaca", "feed": "sip", "adjustment": "raw"}, follow=True)
                follower = Follower(env, provider)
            console(env, follower)
        return 0
    except (DataError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        if provider:
            provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
