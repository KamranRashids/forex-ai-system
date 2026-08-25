"""Timeframe math: supported candle intervals, UTC bucket alignment.

All timestamps are timezone-aware UTC. A bucket is identified by its *start*
time; a bar is "closed" once ``bucket_start + seconds <= now``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final


class Timeframe:
    """Supported candle timeframes (string-valued, DB-safe)."""

    M5: Final[str] = "M5"
    M15: Final[str] = "M15"
    H1: Final[str] = "H1"
    H4: Final[str] = "H4"
    D1: Final[str] = "D1"

    _ORDERED: Final[tuple[str, ...]] = (M5, M15, H1, H4, D1)
    _SECONDS: Final[dict[str, int]] = {
        M5: 5 * 60,
        M15: 15 * 60,
        H1: 60 * 60,
        H4: 4 * 60 * 60,
        D1: 24 * 60 * 60,
    }

    @classmethod
    def values(cls) -> tuple[str, ...]:
        """All supported timeframe codes in ascending duration order."""
        return cls._ORDERED

    @classmethod
    def seconds(cls, timeframe: str) -> int:
        if timeframe not in cls._SECONDS:
            raise ValueError(f"Unsupported timeframe {timeframe!r}")
        return cls._SECONDS[timeframe]

    @classmethod
    def is_valid(cls, timeframe: str) -> bool:
        return timeframe in cls._SECONDS

    @classmethod
    def rank(cls, timeframe: str) -> int:
        """Position in ascending order (M5=0 ... D1=4)."""
        return cls._ORDERED.index(timeframe)


def align_to_bucket(ts: datetime, timeframe: str) -> datetime:
    """Floor ``ts`` to the containing bucket start for the timeframe."""
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    epoch = datetime(1970, 1, 1, tzinfo=ts.tzinfo)
    step = timedelta(seconds=Timeframe.seconds(timeframe))
    remainder = (ts - epoch) % step
    return ts - remainder


def bucket_start_after(ts: datetime, timeframe: str) -> datetime:
    """The first bucket start strictly after ``ts``."""
    return align_to_bucket(ts, timeframe) + timedelta(seconds=Timeframe.seconds(timeframe))


def previous_closed_bucket(now: datetime, timeframe: str) -> datetime:
    """Start of the most recent bucket whose bar is fully closed at ``now``."""
    current_start = align_to_bucket(now, timeframe)
    return current_start - timedelta(seconds=Timeframe.seconds(timeframe))


def is_bar_closed(bucket_start: datetime, now: datetime, timeframe: str) -> bool:
    """True when the bar starting at ``bucket_start`` has closed by ``now``."""
    closes_at = bucket_start + timedelta(seconds=Timeframe.seconds(timeframe))
    return closes_at <= now


def iterate_buckets(
    start_inclusive: datetime, end_exclusive: datetime, timeframe: str
) -> list[datetime]:
    """Bucket starts within [start_inclusive, end_exclusive)."""
    if end_exclusive < start_inclusive:
        return []
    step = timedelta(seconds=Timeframe.seconds(timeframe))
    buckets: list[datetime] = []
    cursor = align_to_bucket(start_inclusive, timeframe)
    while cursor < end_exclusive:
        buckets.append(cursor)
        cursor += step
    return buckets
