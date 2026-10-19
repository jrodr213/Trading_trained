import json
import shlex
from .reporting import encode


def show(value):
    print(json.dumps(value, indent=2, default=encode))


def console(env, follower=None):
    print(env.diagnostics()["label"])
    print("No automatic orders. Commands: prices, status, portfolio, buy SYMBOL QTY, sell SYMBOL QTY,")
    print("orders, cancel ID, next (replay only), export, quit")
    if follower:
        follower.start_background()
    try:
        while True:
            try:
                parts = shlex.split(input("market> "))
                if not parts:
                    continue
                command, *args = parts
                if command == "quit":
                    break
                if command == "prices" and not args:
                    with env.lock:
                        show({s: {"close": b.close, "bar_end": b.end} for s, b in env.latest.items()})
                elif command == "status" and not args:
                    show(env.diagnostics())
                elif command == "portfolio" and not args:
                    show(env.get_portfolio())
                elif command in ("buy", "sell") and len(args) == 2:
                    show(env.submit_order(args[0].upper(), command, int(args[1])))
                elif command == "orders" and not args:
                    show(env.get_orders())
                elif command == "cancel" and len(args) == 1:
                    show({"canceled": env.cancel_order(args[0])})
                elif command == "next" and not args:
                    print("Advanced" if env.advance_replay() else "End of data")
                elif command == "export" and not args:
                    print(env.export_run())
                else:
                    print("Unknown command or wrong arguments")
            except (ValueError, RuntimeError) as exc:
                print(str(exc))
    except (EOFError, KeyboardInterrupt):
        print("\nClosing local experiment")
    finally:
        if follower:
            follower.close()
        env.close()
        print(f"Run exported: {env.export_run()}")
