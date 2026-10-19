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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Historical/delayed SIP data and LOCAL simulated trades only")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Project root containing config.toml and .env")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="SYNTHETIC offline replay; no credentials or orders")
    dl = commands.add_parser("download")
    dl.add_argument("--symbols", nargs="+")
    dl.add_argument("--start", required=True)
    dl.add_argument("--end", required=True)
    dl.add_argument("--output", type=Path, default=Path("data/history.csv"))
    dl.add_argument("--interval", type=int, choices=[1, 5, 15])
    dl.add_argument("--refresh", action="store_true")
    rp = commands.add_parser("replay")
    rp.add_argument("--data", type=Path, required=True)
    rp.add_argument("--session", help="Select one NYSE session, YYYY-MM-DD")
    fw = commands.add_parser("follow")
    fw.add_argument("--symbols", nargs="+")
    fw.add_argument("--interval", type=int, choices=[1, 5, 15])
    args = parser.parse_args(argv)
    root = args.project.resolve()
    provider = None
    try:
        config = Config.load(root / "config.toml")
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
            config = replace(config, interval_minutes=args.interval)
        if args.command == "demo":
            print("SYNTHETIC fixture: deterministic invented prices, not market data")
            bars, meta = create_synthetic(Path(config.data_dir) / "SYNTHETIC.csv")
            config = replace(config, symbols=tuple(meta["symbols"]), interval_minutes=1)
            console(MarketEnvironment(bars, config, dataset=meta))
        elif args.command == "replay":
            bars, meta = load_dataset(local(args.data))
            calendar = NYSE()
            sessions = sorted({calendar.session_for(b.start, b.end) for b in bars})
            session = args.session or sessions[0]
            if len(sessions) > 1 and not args.session:
                print(f"Single-session raw-price account: selecting {session}. Use --session YYYY-MM-DD for another session.")
            bars = [b for b in bars if calendar.session_for(b.start, b.end) == session]
            meta = {**meta, "selected_session": session}
            if not bars:
                raise DataError("No bars in selected session")
            config = replace(config, symbols=tuple(meta["symbols"]), interval_minutes=meta["interval_minutes"])
            show(meta["coverage"])
            console(MarketEnvironment(bars, config, dataset=meta))
        elif args.command == "download":
            # Parse/validate dates before credentials or any network call.
            start, end = utc(args.start), utc(args.end)
            if start >= end:
                raise DataError("start must precede end")
            provider = AlpacaProvider(config, root)
            _, meta = download(provider, config, start, end, local(args.output), args.refresh)
            show({"output": str(local(args.output)), "coverage": meta["coverage"],
                  "effective_start": meta["effective_start"], "effective_end": meta["effective_end"]})
        else:
            provider = AlpacaProvider(config, root)
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
