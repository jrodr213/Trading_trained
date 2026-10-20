from dataclasses import dataclass, asdict
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Config:
    provider: str = "alpaca"
    yahoo_interval: str = "1d"
    yahoo_poll_interval_seconds: float = 120
    yahoo_requests_per_minute: int = 10
    yahoo_backoff_seconds: float = 120
    low_volume_threshold: int = 1000
    yahoo_execution_price_tick: str = "0.000001"
    yahoo_volume_participation: str = "0.10"
    symbols: tuple[str, ...] = ("KO", "JPM", "IBM")
    interval_minutes: int = 1
    delay_minutes: int = 15
    safety_buffer_seconds: int = 30
    poll_interval_seconds: float = 30
    overlap_minutes: int = 2
    timeout_seconds: float = 15
    requests_per_minute: int = 120
    max_retries: int = 3
    initial_cash: str = "100000.00"
    fee_per_order: str = "0.00"
    fee_per_share: str = "0.00"
    slippage_bps: str = "0"
    execution_price_tick: str = "0.01"
    max_order_shares: int = 1000
    stale_tolerance_seconds: int = 120
    data_dir: str = "data"
    runs_dir: str = "runs"

    def __post_init__(self):
        from decimal import Decimal
        if self.provider not in ("alpaca", "yahoo"):
            raise ValueError("Provider must be alpaca or yahoo")
        if self.yahoo_interval not in ("1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "1wk", "1mo"):
            raise ValueError("Unsupported Yahoo interval")
        if self.yahoo_poll_interval_seconds < 30 or not 1 <= self.yahoo_requests_per_minute <= 30 or self.yahoo_backoff_seconds < 30:
            raise ValueError("Yahoo polling/backoff must be >=30s and request budget 1..30/minute")
        if self.low_volume_threshold < 0:
            raise ValueError("Low volume threshold must be nonnegative")
        if not 0 < Decimal(self.yahoo_volume_participation) <= 1:
            raise ValueError("Yahoo volume participation must be in (0,1]")
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Symbols must be nonempty and unique; order is fixed")
        if self.interval_minutes not in (1, 5, 15):
            raise ValueError("Supported intervals: 1, 5, 15 minutes")
        if self.delay_minutes < 15 or self.safety_buffer_seconds < 0:
            raise ValueError("SIP delay must be >=15 minutes with a nonnegative buffer")
        if not 1 <= self.requests_per_minute <= 180:
            raise ValueError("Request budget must be 1..180 per minute")
        if not 0 <= self.max_retries <= 10 or self.timeout_seconds <= 0:
            raise ValueError("Invalid retry/timeout configuration")
        if self.poll_interval_seconds <= 0 or self.overlap_minutes < 0:
            raise ValueError("Polling must be positive and overlap nonnegative")
        if self.max_order_shares <= 0 or self.stale_tolerance_seconds < 0:
            raise ValueError("Invalid order/staleness limit")
        for key in ("initial_cash", "fee_per_order", "fee_per_share", "slippage_bps"):
            value = Decimal(str(getattr(self, key)))
            if not value.is_finite() or value < 0:
                raise ValueError(f"Invalid {key}")
        if Decimal(self.slippage_bps) >= 10000:
            raise ValueError("Slippage must be below 10000 bps")
        tick = Decimal(self.execution_price_tick)
        if not tick.is_finite() or tick <= 0 or tick != Decimal(1).scaleb(tick.as_tuple().exponent):
            raise ValueError("execution_price_tick must be a positive power of ten")

    @classmethod
    def load(cls, path: Path):
        values = tomllib.loads(path.read_text()) if path.exists() else {}
        if "symbols" in values:
            values["symbols"] = tuple(values["symbols"])
        return cls(**values)

    def to_dict(self):
        return asdict(self)
