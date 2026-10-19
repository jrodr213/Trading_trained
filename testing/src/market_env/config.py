from dataclasses import dataclass, asdict
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Config:
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
    max_order_shares: int = 1000
    stale_tolerance_seconds: int = 120
    data_dir: str = "data"
    runs_dir: str = "runs"

    def __post_init__(self):
        from decimal import Decimal
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

    @classmethod
    def load(cls, path: Path):
        values = tomllib.loads(path.read_text()) if path.exists() else {}
        if "symbols" in values:
            values["symbols"] = tuple(values["symbols"])
        return cls(**values)

    def to_dict(self):
        return asdict(self)
