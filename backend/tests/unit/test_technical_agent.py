"""Unit tests for the technical confluence agent."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from app.agents.base import AnalysisContext, Direction
from app.agents.technical import TechnicalAgent

AGENT = TechnicalAgent()
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _frame(bars: int, *, trend: float = 0.0, base: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2026-08-21T08:00:00Z", periods=bars, freq="15min")
    opens, highs, lows, closes = [], [], [], []
    price = base
    for i in range(bars):
        step = 1.0 * (i % 3 - 1) + trend
        o = price
        c = price + step
        highs.append(max(o, c) + 0.8)
        lows.append(min(o, c) - 0.8)
        opens.append(o)
        closes.append(c)
        price = c
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": [1000] * bars},
        index=idx,
    )


def _ctx(
    frame: pd.DataFrame,
    *,
    prev_daily: dict[str, float] | None = None,
    run_id: str = "run-fixed",
) -> AnalysisContext:
    bucket = frame.index[-1].to_pydatetime().replace(tzinfo=UTC)
    return AnalysisContext(
        symbol="EURUSD",
        timeframe="M15",
        bucket_ts=bucket,
        candles=frame,
        now=NOW,
        meta={"prev_daily": prev_daily, "run_id": run_id},
    )


def _trend_frame(bars: int, drift: float) -> pd.DataFrame:
    # Deterministic jitter keeps oscillators (MACD histogram etc.) off exact
    # zero the way real markets are, while preserving the dominant drift.
    def wiggle(i: int) -> float:
        return 0.18 * math.sin(i / 1.7)

    idx = pd.date_range("2026-08-21T06:00:00Z", periods=bars, freq="15min")
    rows_o, rows_h, rows_l, rows_c = [], [], [], []
    p = 100.0
    for i in range(bars):
        o = p
        c = p + drift + wiggle(i)
        rows_h.append(max(o, c) + 0.5)
        rows_l.append(min(o, c) - 0.5)
        rows_o.append(o)
        rows_c.append(c)
        p = c
    return pd.DataFrame(
        {"open": rows_o, "high": rows_h, "low": rows_l, "close": rows_c, "volume": [900] * bars},
        index=idx,
    )


@pytest.mark.unit
def test_strong_uptrend_scores_long() -> None:
    ctx = _ctx(_trend_frame(150, drift=+0.6))
    signal = AGENT.analyze(ctx)
    assert signal.direction is Direction.LONG
    assert signal.confidence >= 0.15
    votes = signal.features["votes"]
    # Structural trend votes must align; oscillator votes may vary with phase.
    assert votes["ema_cross"] > 0
    assert signal.features["score"] > 0
    assert signal.agent_id == "technical"
    assert signal.version == "1"


@pytest.mark.unit
def test_strong_downtrend_scores_short() -> None:
    ctx = _ctx(_trend_frame(150, drift=-0.6))
    signal = AGENT.analyze(ctx)
    assert signal.direction is Direction.SHORT
    assert signal.confidence >= 0.15
    votes = signal.features["votes"]
    assert votes["ema_cross"] < 0


@pytest.mark.unit
def test_flat_market_stays_flat() -> None:
    frame = _frame(150, base=100.0)  # oscillation around a level
    signal = AGENT.analyze(_ctx(frame))
    score = signal.features["score"]
    assert abs(score) < 0.30 or signal.direction is Direction.FLAT


@pytest.mark.unit
def test_insufficient_history_returns_flat_neutral() -> None:
    ctx = _ctx(_trend_frame(20, drift=+1.0))
    signal = AGENT.analyze(ctx)
    assert signal.direction is Direction.FLAT
    assert signal.confidence == 0.0
    assert "insufficient_history" in signal.rationale


@pytest.mark.unit
def test_pivot_vote_uses_prev_daily_when_present() -> None:
    frame = _trend_frame(150, drift=+0.6)
    with_pivot = AGENT.analyze(_ctx(frame, prev_daily={"high": 90.0, "low": 80.0, "close": 85.0}))
    without_pivot = AGENT.analyze(_ctx(frame))
    assert with_pivot.features["votes"]["pivot_position"] != 0
    assert without_pivot.features["votes"]["pivot_position"] == 0
    # Price far above the prior-day pivot adds bullish contribution.
    assert with_pivot.features["votes"]["pivot_position"] > 0


@pytest.mark.unit
def test_adx_gate_scales_trend_votes_in_weak_trend() -> None:  # noqa: PLR0915 - readability
    frame = _frame(150, base=100.0)
    signal = AGENT.analyze(_ctx(frame))
    gate = signal.features["gate"]
    assert 0.3 <= gate <= 1.0


@pytest.mark.unit
def test_signal_freshness_window_is_two_intervals() -> None:
    frame = _trend_frame(150, drift=+0.6)
    signal = AGENT.analyze(_ctx(frame))
    expected_expiry = signal.bucket_ts + timedelta(minutes=30)
    assert signal.valid_until == expected_expiry


@pytest.mark.unit
def test_deterministic_output_for_identical_context() -> None:
    frame = _trend_frame(150, drift=+0.6)
    first = AGENT.analyze(_ctx(frame, run_id="same"))
    second = AGENT.analyze(_ctx(frame, run_id="same"))
    assert first.model_dump() == second.model_dump()


@pytest.mark.unit
def test_features_carry_indicator_snapshot() -> None:
    signal = AGENT.analyze(_ctx(_trend_frame(150, drift=+0.6)))
    indicators = signal.features["indicators"]
    for key in ("ema9", "ema21", "rsi14", "adx14", "atr14", "macd_hist", "close"):
        assert key in indicators
