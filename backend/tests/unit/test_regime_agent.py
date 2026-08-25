"""Unit tests for the regime classification agent (boundary fixtures)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from app.agents.base import AnalysisContext, Direction
from app.agents.regime import RegimeAgent, session_bucket, volatility_bucket

AGENT = RegimeAgent()
NOW = datetime(2026, 8, 20, 13, 0, tzinfo=UTC)  # Wednesday overlap session


def _trending_frame(bars: int = 120) -> pd.DataFrame:
    idx = pd.date_range("2026-08-19T00:00:00Z", periods=bars, freq="15min")
    o, h, low, c = [], [], [], []
    p = 100.0
    for _ in range(bars):
        o.append(p)
        c.append(p + 1.2)
        h.append(p + 1.7)
        low.append(p - 0.4)
        p += 1.2
    return pd.DataFrame(
        {"open": o, "high": h, "low": low, "close": c, "volume": [800] * bars}, index=idx
    )


def _ranging_frame(bars: int = 120) -> pd.DataFrame:
    def hash01(x: float) -> float:
        """Deterministic pseudo-random in [0,1): classic sin-hash."""
        return abs(math.sin(x * 12.9898)) % 1.0

    idx = pd.date_range("2026-08-19T00:00:00Z", periods=bars, freq="15min")
    o, h, low, c = [], [], [], []
    base = 100.0
    for i in range(bars):
        step = 0.6 if i % 2 == 0 else -0.6
        o.append(base)
        c.append(base + step)
        # Independent hash jitter per side -> balanced +/-DM (no trend signal).
        k_high = 0.45 + 0.45 * hash01(i * 1.618)
        k_low = 0.45 + 0.45 * hash01(i * 2.718 + 11.0)
        h.append(max(o[-1], c[-1]) + k_high)
        low.append(min(o[-1], c[-1]) - k_low)
        base = c[-1]
    return pd.DataFrame(
        {"open": o, "high": h, "low": low, "close": c, "volume": [800] * bars}, index=idx
    )


def _ctx(frame: pd.DataFrame, now: datetime = NOW) -> AnalysisContext:
    bucket = frame.index[-1].to_pydatetime().replace(tzinfo=UTC)
    return AnalysisContext(
        symbol="EURUSD", timeframe="M15", bucket_ts=bucket, candles=frame, now=now
    )


@pytest.mark.unit
def test_strong_trend_classifies_trending() -> None:
    signal = AGENT.analyze(_ctx(_trending_frame()))
    assert signal.features["regime"] in {"trending", "weakening_trend"}
    assert signal.features["adx"] >= 20


@pytest.mark.unit
def test_choppy_market_never_trending() -> None:
    signal = AGENT.analyze(_ctx(_ranging_frame()))
    assert signal.features["regime"] in {"range", "transitional"}
    assert signal.features["regime"] != "trending"


@pytest.mark.unit
def test_regime_signal_is_metadata_only() -> None:
    for frame in (_trending_frame(), _ranging_frame()):
        signal = AGENT.analyze(_ctx(frame))
        assert signal.direction is Direction.FLAT
        assert signal.confidence == 0.0
        assert signal.agent_id == "regime"


@pytest.mark.unit
def test_volatility_bucket_high_after_expansion() -> None:
    frame = _ranging_frame()
    # Inject a volatility explosion in the final 10 bars.
    frame.loc[frame.index[-10:], ["high"]] += 6.0
    frame.loc[frame.index[-10:], ["low"]] -= 6.0
    signal = AGENT.analyze(_ctx(frame))
    assert signal.features["volatility"] == "high"


def test_volatility_bucket_terciles() -> None:
    assert volatility_bucket(0.0) == "low"
    assert volatility_bucket(0.30) == "low"
    assert volatility_bucket(1 / 3) == "normal"  # boundary is exclusive-low
    assert volatility_bucket(0.50) == "normal"
    assert volatility_bucket(2 / 3) == "high"  # boundary is exclusive-normal
    assert volatility_bucket(0.99) == "high"


@pytest.mark.unit
def test_session_buckets_by_utc_hour() -> None:
    cases = {
        23: "asia",
        3: "asia",
        7: "london",
        9: "london",
        13: "overlap",
        18: "newyork",
        21: "late",
    }
    for hour, expected in cases.items():
        ts = datetime(2026, 8, 20, hour, 0, tzinfo=UTC)
        ctx = AnalysisContext(
            symbol="EURUSD",
            timeframe="M15",
            bucket_ts=ts,
            candles=_ranging_frame(),
            now=ts,
        )
        assert AGENT.analyze(ctx).features["session"] == expected, f"hour={hour}"
    assert session_bucket(0) == "asia"


@pytest.mark.unit
def test_freshness_window_is_four_intervals() -> None:
    signal = AGENT.analyze(_ctx(_ranging_frame()))
    expected_expiry = signal.bucket_ts + timedelta(minutes=60)
    assert signal.valid_until == expected_expiry


@pytest.mark.unit
def test_insufficient_history_reports_unknown() -> None:
    short = _ranging_frame().iloc[:20]
    signal = AGENT.analyze(_ctx(short))
    assert signal.features["regime"] == "unknown"
    assert signal.direction is Direction.FLAT
