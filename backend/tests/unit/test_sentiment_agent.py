"""Unit tests for the sentiment analysis agent (Phase 4, lexicon fallback)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from app.agents.base import AnalysisContext, Direction
from app.agents.sentiment import SentimentAgent, decay_weight

pytestmark = [pytest.mark.unit]

AGENT = SentimentAgent()
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


def _ctx(news: list[dict]) -> AnalysisContext:
    return AnalysisContext(
        symbol="EURUSD",
        timeframe="M15",
        bucket_ts=NOW,
        candles=_frame(),
        now=NOW,
        meta={"news": news, "run_id": "r"},
    )


def _item(headline: str, *, symbols: list[str] | None = None) -> dict:
    return {
        "headline": headline,
        "published_utc": (NOW - timedelta(hours=1)).isoformat(),
        "symbols": symbols or ["EURUSD"],
        "provider": "synthetic",
    }


def test_decay_weight_is_one_at_zero_age() -> None:
    assert decay_weight(NOW, NOW) == 1.0


def test_decay_weight_decreases_with_age() -> None:
    old = decay_weight(NOW - timedelta(hours=12), NOW)
    fresh = decay_weight(NOW - timedelta(minutes=1), NOW)
    assert 0.0 < old < fresh < 1.0


def test_decay_weight_zero_half_life_is_flat() -> None:
    assert decay_weight(NOW - timedelta(hours=5), NOW, half_life_hours=0) == 1.0


def test_no_news_is_flat_neutral() -> None:
    signal = AGENT.analyze(_ctx([]))
    assert signal.direction is Direction.FLAT
    assert signal.features["item_count"] == 0
    assert signal.agent_id == "sentiment"


def test_positive_base_news_drives_long() -> None:
    # EURGBP touches EUR (base) but not USD (quote) -> positive differential.
    signal = AGENT.analyze(_ctx([_item("Euro surges on strong growth data", symbols=["EURGBP"])]))
    assert signal.direction is Direction.LONG
    assert signal.features["differential"] > 0


def test_positive_quote_news_drives_short() -> None:
    # USDJPY touches USD (quote) but not EUR (base) -> negative differential.
    signal = AGENT.analyze(_ctx([_item("Dollar surges on strong growth data", symbols=["USDJPY"])]))
    assert signal.direction is Direction.SHORT
    assert signal.features["differential"] < 0


def test_malformed_news_is_skipped_safely() -> None:
    news = [
        {},
        {"headline": "plain neutral headline here", "published_utc": "bad-date", "symbols": []},
    ]
    signal = AGENT.analyze(_ctx(news))
    assert signal.direction is Direction.FLAT


def test_naive_published_time_does_not_crash() -> None:
    news = [
        _item("neutral words here", symbols=["EURGBP"]) | {"published_utc": "2026-08-27T10:00:00"}
    ]
    signal = AGENT.analyze(_ctx(news))
    assert signal.direction in (Direction.LONG, Direction.SHORT, Direction.FLAT)
