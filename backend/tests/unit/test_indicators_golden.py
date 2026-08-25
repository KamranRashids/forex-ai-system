"""Golden-value tests for the indicator library.

Expected numbers are computed by hand from the documented definitions in
``app/agents/indicators.py`` — the tests do not re-implement the algorithms.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from app.agents.indicators import (
    adx,
    atr,
    bollinger,
    donchian,
    ema,
    floor_pivots,
    last_valid,
    macd,
    rsi,
    sma,
    stochastic,
    true_range,
)


def _series(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype=float)


def _ohlc(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


class TestSMA:
    def test_sma5_known_values(self) -> None:
        s = _series([1, 2, 3, 4, 5, 6, 7])
        out = sma(s, 5)
        assert math.isnan(out.iloc[3])
        assert out.iloc[4] == pytest.approx(3.0)  # (1+2+3+4+5)/5
        assert out.iloc[5] == pytest.approx(4.0)
        assert out.iloc[6] == pytest.approx(5.0)

    def test_sma_short_series_all_nan(self) -> None:
        out = sma(_series([1.0, 2.0]), 5)
        assert out.isna().all()


class TestEMA:
    def test_ema_seeded_with_first_value(self) -> None:
        # alpha = 2/(3+1) = 0.5; recursion starts from y_0 = x_0 (seed),
        # min_periods=3 masks the first two slots.
        s = _series([10, 20, 30, 40])
        out = ema(s, 3)
        assert math.isnan(out.iloc[0])
        assert math.isnan(out.iloc[1])
        # y_0=10, y_1=.5*20+.5*10=15, y_2=.5*30+.5*15=22.5.
        assert out.iloc[2] == pytest.approx(22.5)
        assert out.iloc[3] == pytest.approx(0.5 * 40 + 0.5 * 22.5)

    def test_ema_constant_series_converges_to_constant(self) -> None:
        out = ema(_series([5.0] * 10), 4)
        assert out.dropna().eq(5.0).all()


class TestRSI:
    def test_rsi_all_gains_is_100(self) -> None:
        closes = [float(i) for i in range(1, 21)]  # strictly increasing
        out = rsi(_series(closes), length=14)
        assert out.iloc[-1] == pytest.approx(100.0)

    def test_rsi_all_losses_is_zero(self) -> None:
        closes = [float(-i) for i in range(1, 21)]
        out = rsi(_series(closes), length=14)
        assert out.iloc[-1] == pytest.approx(0.0)

    def test_rsi_hand_computed_mixed_case(self) -> None:
        # 16 closes -> 15 diffs; Wilder seed at diff index 14 (close idx 15).
        closes = [100, 101, 102, 101, 103, 104, 103, 105, 106, 105, 107, 108, 107, 109, 110, 109]
        diffs = np.diff(closes).astype(float)
        gains = np.where(diffs > 0, diffs, 0.0)
        losses = np.where(diffs < 0, -diffs, 0.0)
        # Seed: simple mean of first 14 gains/losses.
        ag_seed = gains[:14].mean()
        al_seed = losses[:14].mean()
        alpha = 1 / 14
        # One more Wilder step with the 15th diff.
        ag = alpha * gains[14] + (1 - alpha) * ag_seed
        al = alpha * losses[14] + (1 - alpha) * al_seed
        expected = 100 - 100 / (1 + ag / al)

        out = rsi(_series([float(c) for c in closes]), length=14)
        assert out.iloc[-1] == pytest.approx(expected)


class TestMACD:
    def test_macd_line_is_ema12_minus_ema26(self) -> None:
        rng = np.random.default_rng(7)
        closes = pd.Series(100 + rng.normal(0, 1, 80).cumsum())
        line, signal, hist = macd(closes)
        manual = ema(closes, 12) - ema(closes, 26)
        assert line.iloc[-1] == pytest.approx(manual.iloc[-1])
        # signal = EMA9 of the macd line; hist = line - signal.
        manual_signal = line.ewm(alpha=2 / 10, adjust=False).mean()
        assert signal.iloc[-1] == pytest.approx(manual_signal.iloc[-1])
        assert hist.iloc[-1] == pytest.approx(line.iloc[-1] - signal.iloc[-1])

    def test_warmup_nan_until_slow_period(self) -> None:
        line, _signal, _hist = macd(_series([1.0] * 25), fast=12, slow=26, signal_len=9)
        assert line.isna().all() or line.first_valid_index() is None


class TestBollinger:
    def test_population_std_bands_hand_computed(self) -> None:
        closes = [2.0, 4.0, 6.0, 8.0]
        s = _series(closes + [10.0, 12.0, 14.0, 16.0, 18.0, 20.0])
        mid, upper, lower = bollinger(s, length=4, num_std=2.0)
        i = 6  # window [10,12,14,16]? no -> last four values 12,14,16,18? verify below
        _ = i
        window = s.iloc[3:7]  # rolling at index 6 covers indices 3..6
        mean = window.mean()
        std = window.std(ddof=0)
        assert mid.iloc[6] == pytest.approx(mean)
        assert upper.iloc[6] == pytest.approx(mean + 2 * std)
        assert lower.iloc[6] == pytest.approx(mean - 2 * std)

    def test_flat_series_has_zero_width(self) -> None:
        s = _series([50.0] * 25)
        mid, upper, lower = bollinger(s, 20, 2)
        assert mid.iloc[-1] == pytest.approx(50.0)
        assert upper.iloc[-1] == pytest.approx(50.0)
        assert lower.iloc[-1] == pytest.approx(50.0)


class TestTrueRangeATR:
    def test_true_range_picks_max_component(self) -> None:
        df = _ohlc([(0, 0, 0, 0), (0, 11, 9, 10)])  # prev close row0 = 0
        tr = true_range(df["high"], df["low"], df["close"])
        # row0: high-low = 0; row1: max(|11-9|, |11-0|, |9-0|) = 11
        assert tr.iloc[0] == pytest.approx(0.0)
        assert tr.iloc[1] == pytest.approx(11.0)

    def test_atr_wilder_seed_is_sma_of_tr(self) -> None:
        highs = _series([10, 11, 12, 13, 14, 15])
        lows = _series([9, 10, 11, 12, 13, 14])
        closes = _series([9.5, 10.5, 11.5, 12.5, 13.5, 14.5])
        df = pd.DataFrame({"high": highs, "low": lows, "close": closes})
        tr = true_range(highs, lows, closes)
        out = atr(df, length=3)
        assert out.iloc[2] == pytest.approx(tr.iloc[:3].mean())
        # Manual Wilder step for index 3.
        alpha = 1 / 3
        expected3 = alpha * tr.iloc[3] + (1 - alpha) * tr.iloc[:3].mean()
        assert out.iloc[3] == pytest.approx(expected3)


class TestStochastic:
    def test_close_at_range_extremes(self) -> None:
        # Ascending OHLC: open=low=i, close=high=i+1.
        df = pd.DataFrame(
            {
                "open": [float(i) for i in range(15)],
                "high": [float(i) + 1 for i in range(15)],
                "low": [float(i) - 1 for i in range(15)],
                "close": [float(i) for i in range(15)],
            }
        )
        k, d = stochastic(df, k_length=5, k_smooth=1, d_smooth=1)
        # At the last bar: close=14, HH=14+1=15 over last 5 bars (10..14),
        # LL=(10-1)=9 -> %K = 100*(14-9)/(15-9) = 83.33...
        assert k.iloc[-1] == pytest.approx(100 * (14 - 9) / (15 - 9))
        assert d.iloc[-1] == pytest.approx(k.iloc[-1])

    def test_smoothed_k_then_d(self) -> None:
        df = pd.DataFrame(
            {
                "open": np.linspace(1, 30, 30),
                "high": np.linspace(2, 31, 30),
                "low": np.linspace(0, 29, 30),
                "close": np.linspace(1.5, 30.5, 30),
            }
        )
        slow_k, slow_d = stochastic(df, k_length=14, k_smooth=3, d_smooth=3)
        # Slow %K becomes valid two bars after raw %K's first valid slot;
        # %D lags slow %K by another two bars.
        assert slow_k.first_valid_index() is not None
        first_k = slow_k.first_valid_index()
        first_d = slow_d.first_valid_index()
        assert first_d is not None and first_k is not None
        assert df.index.get_loc(first_d) == df.index.get_loc(first_k) + (3 - 1)


class TestADX:
    def test_strong_trend_yields_high_adx_and_di_split(self) -> None:
        rows = []
        price = 100.0
        for _i in range(60):
            high = price + 1.5
            low = price - 0.5
            rows.append((price, high, low, price + 1.0))
            price += 1.0
        df = _ohlc(rows)
        adx_line, plus_di, minus_di = adx(df, length=14)
        assert not np.isnan(adx_line.iloc[-1])
        assert plus_di.iloc[-1] > minus_di.iloc[-1]
        assert 0 <= adx_line.iloc[-1] <= 100

    def test_di_bounds(self) -> None:
        rows = [
            (100 + (i % 5), 101 + (i % 7), 99 - (i % 3), 100 + ((i * 3) % 4)) for i in range(60)
        ]
        df = _ohlc(rows)
        adx_line, plus_di, minus_di = adx(df, length=14)
        for series in (adx_line, plus_di, minus_di):
            tail = series.dropna()
            assert ((tail >= 0) & (tail <= 100)).all()

    def test_choppy_market_adx_lower_than_trend_adx(self) -> None:
        trend_rows = []
        chop_rows = []
        p = 100.0
        for _i in range(80):
            trend_rows.append((p, p + 2.0, p - 1.0, p + 1.0))
            p += 1.0
        base = 100.0
        for i in range(80):
            step = 2.0 if i % 2 == 0 else -2.0
            o = base
            c = base + step
            # Ranges drift so both +DM and -DM stay nonzero (realistic chop).
            high = max(o, c) + 1.0 + (i % 4)
            low = min(o, c) - 1.0 - ((i + 2) % 4)
            chop_rows.append((o, high, low, c))
            base = c
        trend_adx, _, _ = adx(_ohlc(trend_rows), length=14)
        chop_adx, _, _ = adx(_ohlc(chop_rows), length=14)
        assert not np.isnan(trend_adx.iloc[-1])
        assert not np.isnan(chop_adx.iloc[-1])
        assert trend_adx.iloc[-1] > chop_adx.iloc[-1]


class TestDonchian:
    def test_channel_bounds(self) -> None:
        df = pd.DataFrame(
            {
                "open": np.arange(1, 31, dtype=float),
                "high": np.arange(2, 32, dtype=float),
                "low": np.arange(0, 30, dtype=float),
                "close": np.arange(1.5, 31.5, dtype=float),
            }
        )
        upper, mid, lower = donchian(df, length=10)
        assert upper.iloc[-1] == pytest.approx(df["high"].iloc[-10:].max())
        assert lower.iloc[-1] == pytest.approx(df["low"].iloc[-10:].min())
        assert mid.iloc[-1] == pytest.approx((upper.iloc[-1] + lower.iloc[-1]) / 2)


class TestFloorPivots:
    def test_classic_formula_hand_computed(self) -> None:
        levels = floor_pivots(prev_high=110.0, prev_low=100.0, prev_close=105.0)
        p = (110 + 100 + 105) / 3  # 105.0
        assert levels.pivot == pytest.approx(p)
        assert levels.r1 == pytest.approx(2 * p - 100)
        assert levels.s1 == pytest.approx(2 * p - 110)
        assert levels.r2 == pytest.approx(p + 10)
        assert levels.s2 == pytest.approx(p - 10)
        assert levels.r3 == pytest.approx(110 + 2 * (p - 100))
        assert levels.s3 == pytest.approx(100 - 2 * (110 - p))


def test_last_valid_helper() -> None:
    assert last_valid(pd.Series([np.nan, 1.0, np.nan])) == pytest.approx(1.0)
    assert last_valid(pd.Series(dtype=float)) is None
    assert last_valid(pd.Series([np.nan])) is None
