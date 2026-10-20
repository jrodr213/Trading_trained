"""Provider selection; Alpaca remains the default and retains its API."""
from typing import Protocol
from datetime import datetime
from .data import Bar, AlpacaProvider


class MarketDataProvider(Protocol):
    def fetch(self, symbols, start: datetime, end: datetime) -> tuple[list[Bar], datetime]: ...
    def close(self) -> None: ...


def make_provider(config, root, **kwargs) -> MarketDataProvider:
    if config.provider == "alpaca":
        return AlpacaProvider(config, root, **kwargs)
    from .yahoo import YahooProvider
    return YahooProvider(config, root, **kwargs)
