"""Unit tests for timeframe math and bucket alignment."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.data.timeframes import (
    Timeframe,
    align_to_bucket,
    bucket_start_after,
    is_bar_closed,
    iterate_buckets,
    previous_closed_bucket,
)


@pytest.mark.unit
def test_supported_timeframes_and_seconds() -> None:
    assert Timeframe.values() == ("M5", "M15", "H1", "H4", "D1")
    assert Timeframe.seconds("M5") == 300
    assert Timeframe.seconds("M15") == 900
    assert Timeframe.seconds("H1") == 3600
    assert Timeframe.seconds("H4") == 14400
    assert Timeframe.seconds("D1") == 86400
    with pytest.raises(ValueError, match="Unsupported timeframe"):
        Timeframe.seconds("M1")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "timeframe", "expected"),
    [
        ("2026-08-24T10:07:31Z", "M15", "2026-08-24T10:00:00Z"),
        ("2026-08-24T10:00:00Z", "M15", "2026-08-24T10:00:00Z"),
        ("2026-08-24T10:59:59Z", "H1", "2026-08-24T10:00:00Z"),
        ("2026-08-24T03:59:59Z", "H4", "2026-08-24T00:00:00Z"),
        ("2026-08-24T05:00:01Z", "H4", "2026-08-24T04:00:00Z"),
        ("2026-08-24T23:11:00Z", "D1", "2026-08-24T00:00:00Z"),
        ("2026-08-24T09:02:13Z", "M5", "2026-08-24T09:00:00Z"),
    ],
)
def test_align_to_bucket(raw: str, timeframe: str, expected: str) -> None:
    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    expected_ts = datetime.fromisoformat(expected.replace("Z", "+00:00"))
    assert align_to_bucket(ts, timeframe) == expected_ts


@pytest.mark.unit
def test_align_requires_timezone_aware() -> None:
    naive = datetime(2026, 8, 24, 10, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        align_to_bucket(naive, "M15")


@pytest.mark.unit
def test_previous_closed_bucket() -> None:
    now = datetime(2026, 8, 24, 10, 7, 0, tzinfo=UTC)
    assert previous_closed_bucket(now, "M15") == datetime(2026, 8, 24, 9, 45, tzinfo=UTC)
    exactly_open = datetime(2026, 8, 24, 10, 0, 0, tzinfo=UTC)
    assert previous_closed_bucket(exactly_open, "M15") == datetime(2026, 8, 24, 9, 45, tzinfo=UTC)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("bucket", "now", "timeframe", "closed"),
    [
        ("2026-08-24T09:45:00Z", "2026-08-24T10:00:00Z", "M15", True),
        ("2026-08-24T09:45:00Z", "2026-08-24T09:59:59Z", "M15", False),
        ("2026-08-24T09:45:00Z", "2026-08-24T09:45:00Z", "M15", False),
        ("2026-08-21T00:00:00Z", "2026-08-22T00:00:00Z", "D1", True),
    ],
)
def test_is_bar_closed(bucket: str, now: str, timeframe: str, closed: bool) -> None:
    b = datetime.fromisoformat(bucket.replace("Z", "+00:00"))
    n = datetime.fromisoformat(now.replace("Z", "+00:00"))
    assert is_bar_closed(b, n, timeframe) is closed


@pytest.mark.unit
def test_iterate_buckets_bounds() -> None:
    start = datetime(2026, 8, 24, 10, 3, tzinfo=UTC)
    end = datetime(2026, 8, 24, 10, 33, tzinfo=UTC)
    buckets = iterate_buckets(start, end, "M15")
    assert buckets[0] == datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    assert buckets[-1] == datetime(2026, 8, 24, 10, 30, tzinfo=UTC)
    assert len(buckets) == 3
    assert iterate_buckets(end, start, "M15") == []


@pytest.mark.unit
def test_bucket_start_after() -> None:
    inside = datetime(2026, 8, 24, 10, 7, 0, tzinfo=UTC)
    assert bucket_start_after(inside, "M15") == datetime(2026, 8, 24, 10, 15, tzinfo=UTC)
