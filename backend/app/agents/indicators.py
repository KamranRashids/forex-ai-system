"""Technical indicator library (hand-rolled on pandas/numpy — no pandas-ta).

Mathematical definitions (authoritative for this codebase):

SMA(n)          : arithmetic mean of the trailing ``n`` values.
EMA(n)          : recursive exponential mean ``y_t = alpha*x_t + (1-alpha)*y_{t-1}``
                  with ``alpha = 2/(n+1)``; seeded with the first value of the
                  series (standard MACD convention), ``adjust=False`` semantics.
Wilder smooth   : like EMA but ``alpha = 1/n`` and *seeded* with the SMA of the
                  first ``n`` values (used by RSI/ATR/ADX).
RSI(n)  [Wilder]: up/down moves of consecutive closes; avg_gain/avg_loss via
                  Wilder smoothing; ``RS = AG/AL``; ``RSI = 100 - 100/(1+RS)``
                  (RSI = 100 when AL == 0; RSI = 50-neutral band handled by
                  callers, library returns pure math).
MACD(f,s,g)     : ``macd = EMA_f - EMA_s``; ``signal = EMA_g(macd)``;
                  ``hist = macd - signal``.
Bollinger(n,k)  : mid = SMA_n; spread uses *population* std (ddof=0);
                  upper = mid + k*std; lower = mid - k*std.
TR              : ``max(h-l, |h-prev_close|, |l-prev_close|)``; first row is
                  ``h-l``.
ATR(n)  [Wilder]: Wilder-smoothed TR.
Stochastic      : raw %K = 100*(c - LL_n)/(HH_n - LL_n); slow %K = SMA_m(raw K);
                  %D = SMA_m(slow %K). Defaults n=14, m=3.
+DM/-DM         : +DM = h_t - h_{t-1} if strictly greater than l_{t-1} - l_t
                  and positive, else 0; symmetric for -DM.
ADX(n)  [Wilder]: +DI = 100 * Wilder(+DM)/Wilder(TR); -DI symmetric;
                  DX = 100*|+DI - -DI| / (+DI + -DI); ADX = Wilder(DX).

All functions are pure, deterministic, and return Series aligned to the input
index with ``NaN`` during warm-up windows.
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np
import pandas as pd


def sma(series: pd.Series, length: int) -> pd.Series:
    """Arithmetic mean of trailing ``length`` values."""
    return series.rolling(window=length, min_periods=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential mean, alpha=2/(length+1), seeded with first value."""
    return series.ewm(alpha=2.0 / (length + 1), adjust=False, min_periods=length).mean()


def _wilder_smooth(values: pd.Series, length: int) -> pd.Series:
    """SMA-seeded Wilder smoothing (alpha = 1/length)."""
    smoothed = values.copy().astype(float)
    smoothed.iloc[:length] = np.nan
    if len(values) < length:
        return smoothed
    seed = values.iloc[:length].mean()
    result = np.empty(len(values), dtype=float)
    result[: length - 1] = np.nan
    result[length - 1] = seed
    alpha = 1.0 / length
    raw = values.to_numpy(dtype=float)
    for i in range(length, len(values)):
        result[i] = alpha * raw[i] + (1 - alpha) * result[i - 1]
    return pd.Series(result, index=values.index)


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = _wilder_smooth(gain, length)
    avg_loss = _wilder_smooth(loss, length)

    def _compute(ag: pd.Series, al: pd.Series) -> pd.Series:
        rs = ag / al.replace(0.0, np.nan)
        out = 100.0 - 100.0 / (1.0 + rs)
        out = out.where(~((al == 0) & (ag > 0)), 100.0)
        return out

    return _compute(avg_gain, avg_loss)


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal_len: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd line, signal line, histogram)."""
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(alpha=2.0 / (signal_len + 1), adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger(
    close: pd.Series, length: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger bands with population standard deviation (ddof=0)."""
    mid = sma(close, length)
    std = close.rolling(window=length, min_periods=length).std(ddof=0)
    return mid, mid + num_std * std, mid - num_std * std


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    tr = ranges.max(axis=1)
    tr.iloc[0] = (high.iloc[0] - low.iloc[0]) if len(tr) else np.nan
    return tr


def atr(data: pd.DataFrame, length: int = 14) -> pd.Series:
    """Average True Range (Wilder smoothing). Requires high/low/close columns."""
    tr = true_range(data["high"], data["low"], data["close"])
    return _wilder_smooth(tr, length)


def stochastic(
    data: pd.DataFrame, k_length: int = 14, k_smooth: int = 3, d_smooth: int = 3
) -> tuple[pd.Series, pd.Series]:
    """Slow Stochastic: returns (slow %K, %D)."""
    lowest = data["low"].rolling(k_length, min_periods=k_length).min()
    highest = data["high"].rolling(k_length, min_periods=k_length).max()
    denom = (highest - lowest).replace(0.0, np.nan)
    raw_k = 100.0 * (data["close"] - lowest) / denom
    slow_k = raw_k.rolling(k_smooth, min_periods=k_smooth).mean()
    slow_d = slow_k.rolling(d_smooth, min_periods=d_smooth).mean()
    return slow_k.fillna(value=np.nan), slow_d


def directional_movement(
    data: pd.DataFrame, length: int = 14
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Wilder ADX system: returns (adx, +DI, -DI)."""
    high, low = data["high"], data["low"]
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=data.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=data.index
    )

    tr = true_range(high, low, data["close"])
    atr_smooth = _wilder_smooth(tr, length)
    plus_di = 100.0 * _wilder_smooth(plus_dm, length) / atr_smooth
    minus_di = 100.0 * _wilder_smooth(minus_dm, length) / atr_smooth

    di_sum = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum.replace(0.0, np.nan)
    # Wilder's ADX smooths DX; reindex keeps NaN warmup honest on the input grid.
    adx_line = _wilder_smooth(dx.dropna(), length).reindex(data.index)
    return adx_line, plus_di, minus_di


def adx(data: pd.DataFrame, length: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Public alias returning (adx, +DI, -DI)."""
    return directional_movement(data, length)


def donchian(data: pd.DataFrame, length: int = 20) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Donchian channels: returns (upper, mid, lower) over trailing window."""
    upper = data["high"].rolling(length, min_periods=length).max()
    lower = data["low"].rolling(length, min_periods=length).min()
    return upper, (upper + lower) / 2.0, lower


class PivotLevels(NamedTuple):
    pivot: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float


def floor_pivots(prev_high: float, prev_low: float, prev_close: float) -> PivotLevels:
    """Classic floor-trader pivots from a prior period's H/L/C.

    P  = (H + L + C) / 3
    R1 = 2P - L ; S1 = 2P - H
    R2 = P + (H - L) ; S2 = P - (H - L)
    R3 = H + 2(P - L) ; S3 = L - 2(H - P)
    """
    p = (prev_high + prev_low + prev_close) / 3.0
    hl = prev_high - prev_low
    return PivotLevels(
        pivot=p,
        r1=2 * p - prev_low,
        r2=p + hl,
        r3=prev_high + 2 * (p - prev_low),
        s1=2 * p - prev_high,
        s2=p - hl,
        s3=prev_low - 2 * (prev_high - p),
    )


def last_valid(series: pd.Series) -> float | None:
    """Latest non-NaN value as float, or None when the series is all-NaN."""
    if series.empty:
        return None
    dropped = series.dropna()
    if dropped.empty:
        return None
    return float(dropped.iloc[-1])
