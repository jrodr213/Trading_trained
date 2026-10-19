from datetime import datetime, timedelta, timezone
from typing import Protocol
from functools import lru_cache
import pandas_market_calendars as mcal

UTC = timezone.utc


def utc(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamp must have an explicit timezone")
    return result.astimezone(UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...


class WallClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    def __init__(self, value: datetime):
        self.value = utc(value)

    def now(self) -> datetime:
        return self.value


def safe_cutoff(clock: Clock, config) -> datetime:
    return utc(clock.now()) - timedelta(minutes=config.delay_minutes, seconds=config.safety_buffer_seconds)


class NYSE:
    def __init__(self):
        self.calendar = mcal.get_calendar("NYSE")

    @lru_cache(maxsize=1024)
    def sessions(self, start: str, end: str):
        schedule = self.calendar.schedule(start_date=start, end_date=end)
        return tuple((str(day.date()), row.market_open.to_pydatetime(), row.market_close.to_pydatetime())
                     for day, row in schedule.iterrows())

    def around(self, at: datetime):
        at = utc(at)
        return self.sessions((at - timedelta(days=10)).date().isoformat(),
                             (at + timedelta(days=2)).date().isoformat())

    def session_for(self, start: datetime, end: datetime):
        return next((day for day, opening, closing in self.around(start)
                     if opening <= start < end <= closing), None)

    def is_open(self, at: datetime) -> bool:
        return any(opening <= at < closing for _, opening, closing in self.around(at))

    def expected_end(self, cutoff: datetime, minutes: int) -> datetime | None:
        delta = timedelta(minutes=minutes)
        eligible = []
        for _, opening, closing in self.around(cutoff):
            end = min(closing, cutoff)
            count = (end - opening) // delta
            if count >= 1:
                eligible.append(opening + count * delta)
        return max(eligible, default=None)
