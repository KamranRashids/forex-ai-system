"""Unit tests for the FX market-hours calendar."""

from __future__ import annotations

from datetime import datetime

import pytest
from app.data.market_hours import is_market_open, next_close, next_open


def _ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


@pytest.mark.parametrize(
    ("raw", "open_"),
    [
        ("2026-08-21T12:00:00Z", True),  # Friday midday
        ("2026-08-21T21:59:00Z", True),  # Friday just before close
        ("2026-08-21T22:00:00Z", False),  # Friday close
        ("2026-08-22T12:00:00Z", False),  # Saturday
        ("2026-08-23T09:00:00Z", False),  # Sunday morning
        ("2026-08-23T21:59:00Z", False),  # Sunday just before open
        ("2026-08-23T22:00:00Z", True),  # Sunday open
        ("2026-08-19T03:07:00Z", True),  # Wednesday night
        ("2026-08-24T00:00:00Z", True),  # Monday midnight
    ],
)
def test_market_open_windows(raw: str, open_: bool) -> None:
    assert is_market_open(_ts(raw)) is open_


def test_next_open_from_saturday_lands_sunday_evening() -> None:
    result = next_open(_ts("2026-08-22T12:00:00Z"))
    assert result == _ts("2026-08-23T22:00:00Z")


def test_next_close_from_wednesday_lands_friday() -> None:
    result = next_close(_ts("2026-08-19T12:00:00Z"))
    assert result.date() == datetime(2026, 8, 21).date()
    assert result.hour == 22 and result.minute == 0


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        is_market_open(datetime(2026, 8, 19, 12, 0))
