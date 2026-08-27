"""Unit tests for the calendar-proximity impact model (Phase 4 fallback)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.llm.fallback.calendar_proximity import (
    event_impact,
    event_window,
    proximity_bucket,
)

pytestmark = [pytest.mark.unit]

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def test_peak_impact_at_event_time() -> None:
    imp = event_impact(_NOW, _NOW, "high")
    assert imp is not None
    assert imp.impact == 1.0
    assert imp.within_window is True
    assert imp.window_minutes == 90


def test_impact_decays_with_distance() -> None:
    near = event_impact(_NOW + timedelta(minutes=45), _NOW, "high")
    assert near is not None
    assert 0.45 <= near.impact < 0.55


def test_outside_window_returns_none() -> None:
    assert event_impact(_NOW + timedelta(minutes=200), _NOW, "high") is None


def test_low_importance_returns_none() -> None:
    assert event_impact(_NOW + timedelta(minutes=1), _NOW, "low") is None


def test_custom_half_window_respected() -> None:
    imp = event_impact(_NOW + timedelta(minutes=60), _NOW, "high", half_window_minutes=120)
    assert imp is not None
    assert imp.window_minutes == 120


def test_event_window_returns_timedelta_inside() -> None:
    window = event_window("high", _NOW, _NOW)
    assert window == timedelta(minutes=90)


def test_event_window_none_outside() -> None:
    assert event_window("high", _NOW, _NOW + timedelta(minutes=500)) is None


def test_proximity_bucket_empty_safe_defaults() -> None:
    bucket = proximity_bucket([], _NOW)
    assert bucket["impact"] == 0.0
    assert bucket["nearest_ts"] is None
    assert bucket["importance"] is None


def test_proximity_bucket_picks_max_impact() -> None:
    far_ts = _NOW + timedelta(minutes=80)
    near_ts = _NOW + timedelta(minutes=5)
    bucket = proximity_bucket(
        [(far_ts, "high"), (near_ts, "high")],
        _NOW,
    )
    assert bucket["impact"] > 0.9  # the near one dominates
    assert bucket["nearest_ts"] == near_ts
    assert bucket["importance"] == "high"
