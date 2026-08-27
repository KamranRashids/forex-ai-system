"""Unit tests for the fundamental analysis agent (Phase 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from app.agents.base import AnalysisContext, Direction
from app.agents.fundamental import (
    FundamentalAgent,
    currencies_of,
    parse_number,
    surprise_direction,
)

pytestmark = [pytest.mark.unit]

AGENT = FundamentalAgent()
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _frame() -> pd.DataFrame:
    idx = pd.date_range(str(NOW - timedelta(minutes=45)), periods=4, freq="15min")
    return pd.DataFrame(
        {
            "open": [1.0] * 4,
            "high": [1.0] * 4,
            "low": [1.0] * 4,
            "close": [1.0] * 4,
            "volume": [1] * 4,
        },
        index=idx,
    )


def _ctx(events: list[dict]) -> AnalysisContext:
    return AnalysisContext(
        symbol="EURUSD",
        timeframe="M15",
        bucket_ts=NOW,
        candles=_frame(),
        now=NOW,
        meta={"events": events, "run_id": "r"},
    )


def test_parse_number_handles_percent_and_comma() -> None:
    assert parse_number("3.2%") == 3.2
    assert parse_number("3,5%") == 3.5
    assert parse_number("") is None
    assert parse_number(None) is None
    assert parse_number("n/a") is None


def test_surprise_direction_signs() -> None:
    assert surprise_direction(3.2, 3.1) == 1
    assert surprise_direction(3.0, 3.1) == -1
    assert surprise_direction(3.0, 3.0) == 0
    assert surprise_direction(None, 3.0) == 0
    assert surprise_direction(3.0, None) == 0


def test_currencies_of() -> None:
    assert currencies_of("EURUSD") == ("EUR", "USD")
    assert currencies_of("USDJPY") == ("USD", "JPY")


def test_no_events_is_flat_neutral() -> None:
    signal = AGENT.analyze(_ctx([]))
    assert signal.direction is Direction.FLAT
    assert signal.features["event_count"] == 0
    assert signal.agent_id == "fundamental"


def test_positive_base_surprise_drives_long() -> None:
    events = [
        {
            "timestamp_utc": (NOW - timedelta(hours=1)).isoformat(),
            "importance": "high",
            "currency": "EUR",
            "actual": "3.5%",
            "forecast": "3.0%",
        }
    ]
    signal = AGENT.analyze(_ctx(events))
    assert signal.direction is Direction.LONG
    assert signal.features["base_bias"] > 0


def test_very_close_high_impact_event_suppresses_directional_bet() -> None:
    events = [
        {
            "timestamp_utc": NOW.isoformat(),
            "importance": "high",
            "currency": "EUR",
            "actual": "3.5%",
            "forecast": "3.0%",
        }
    ]
    signal = AGENT.analyze(_ctx(events))
    assert signal.features["impact"] >= 0.85
    assert signal.direction is Direction.FLAT
    assert signal.confidence < 0.5


def test_malformed_event_is_skipped_safely() -> None:
    events = [
        {},
        {"timestamp_utc": "not-a-date", "importance": "medium"},
    ]
    signal = AGENT.analyze(_ctx(events))
    assert signal.direction is Direction.FLAT
    assert signal.features["event_count"] == 2  # both counted but unimpactful
