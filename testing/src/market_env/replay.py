from datetime import timedelta
from decimal import Decimal
from itertools import groupby
from .clock import utc, FrozenClock
from .data import Bar, write_dataset, metadata
from .config import Config


def groups(bars, symbols):
    order = {symbol: i for i, symbol in enumerate(symbols)}
    ordered = sorted(bars, key=lambda b: (b.end, order[b.symbol]))
    return [list(group) for _, group in groupby(ordered, key=lambda b: b.end)]


def synthetic_bars():
    """SYNTHETIC deterministic setup fixture; not real market prices."""
    start = utc("2026-09-01T13:30:00Z")
    bars = []
    for i in range(8):
        for j, symbol in enumerate(("KO", "JPM", "IBM")):
            price = Decimal(60 + j * 70) + Decimal(i) / 10
            at = start + timedelta(minutes=i)
            bars.append(Bar(symbol, at, at + timedelta(minutes=1), price,
                            price + 1, price - 1, price + Decimal("0.25"), Decimal(1000 + i)))
    return bars


def create_synthetic(path):
    bars = synthetic_bars()
    clock = FrozenClock(utc("2026-09-02T00:00:00Z"))
    meta = metadata(Config(), bars, bars[0].start, bars[-1].end, bars[-1].end, clock, synthetic=True)
    return bars, write_dataset(path, bars, meta)
