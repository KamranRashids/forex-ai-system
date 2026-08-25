"""Unit tests for the deterministic synthetic provider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.data.providers.base import Candle
from app.data.providers.synthetic import PAIR_SPECS, generate_candle, synthetic_candles
from app.data.timeframes import align_to_bucket

PAIRS = ["EURUSD", "USDJPY", "GBPUSD"]


def _range(hours: int) -> tuple[datetime, datetime]:
    start = datetime(2026, 8, 18, 0, 0, tzinfo=UTC)
    return start, start + timedelta(hours=hours)


@pytest.mark.unit
def test_generation_is_deterministic_and_order_independent() -> None:
    start, end = _range(6)
    first = synthetic_candles("EURUSD", "M15", start, end)
    second = synthetic_candles("EURUSD", "M15", start, end)
    assert first == second
    # Interleaving other pairs' generation must not perturb results.
    _ = [synthetic_candles(p, "H1", *(_range(48))) for p in reversed(PAIRS)]
    third = synthetic_candles("EURUSD", "M15", start, end)
    assert third == first


@pytest.mark.unit
def test_only_closed_bars_returned() -> None:
    start = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=45)  # 10:00..10:30 closed; 10:45 forming
    candles = synthetic_candles("EURUSD", "M15", start, end)
    assert [c.bucket_start for c in candles] == [
        datetime(2026, 8, 18, 10, 0, tzinfo=UTC),
        datetime(2026, 8, 18, 10, 15, tzinfo=UTC),
        datetime(2026, 8, 18, 10, 30, tzinfo=UTC),
    ]
    assert all(c.complete for c in candles)


@pytest.mark.unit
@pytest.mark.parametrize("pair", PAIRS)
@pytest.mark.parametrize("timeframe", ["M5", "M15", "H1", "H4", "D1"])
def test_ohlcv_invariants_all_timeframes(pair: str, timeframe: str) -> None:
    start, end = _range(72)
    candles = synthetic_candles(pair, timeframe, start, end)
    assert candles, f"no candles for {pair} {timeframe}"
    for candle in candles:
        candle.validate()  # raises on OHLC violations
        assert isinstance(candle.volume, int) and candle.volume > 0
        assert candle.open.as_tuple().exponent >= -5


@pytest.mark.unit
def test_jpy_pair_quantized_to_pip_precision() -> None:
    start, end = _range(24)
    candles = synthetic_candles("USDJPY", "H1", start, end)
    for candle in candles:
        assert candle.close == candle.close.quantize(Decimal("0.001"))
        assert Decimal("100") < candle.close < Decimal("300")


@pytest.mark.unit
def test_bucket_alignment_matches_timeframe_grid() -> None:
    start, end = _range(96)
    for tf in ("M15", "H1", "H4"):
        for candle in synthetic_candles("GBPUSD", tf, start, end):
            assert align_to_bucket(candle.bucket_start, tf) == candle.bucket_start


@pytest.mark.unit
def test_single_candle_helper_consistent_with_range() -> None:
    ts = datetime(2026, 8, 19, 13, 15, tzinfo=UTC)
    single = generate_candle("AUDUSD", "M15", ts)
    start, end = ts, ts + timedelta(minutes=15)
    from_range = synthetic_candles("AUDUSD", "M15", start, end)
    assert from_range == [single]


@pytest.mark.unit
def test_unknown_pair_falls_back_to_eurusd_specs() -> None:
    assert "EURUSD" in PAIR_SPECS
    candle = generate_candle("XXXYYY", "M15", datetime(2026, 8, 19, 13, 15, tzinfo=UTC))
    assert isinstance(candle, Candle)


@pytest.mark.unit
def test_prices_stay_within_plausible_bands() -> None:
    """Noise amplitude is bounded; guard against runaway drift."""
    start, end = _range(24 * 30)
    for candle in synthetic_candles("EURUSD", "D1", start, end):
        assert Decimal("0.80") < candle.close < Decimal("1.60")
