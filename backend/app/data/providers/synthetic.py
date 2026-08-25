"""Deterministic synthetic market data (no credentials, no network).

Used as the default provider for development, CI, and demos (ADR-0003).
Prices come from multi-octave *value noise* evaluated per bucket index, so:

- generation is order-independent and restart-safe: the same
  ``(symbol, timeframe, bucket)`` always yields byte-identical candles;
- arbitrary historical ranges backfill without replaying from genesis;
- consecutive buckets correlate (trending/wavy paths), which is enough
  realism for agents and charts without pretending to be a market model.

This is a *fixture*, not a forecast. It must never be presented as real
market data.
"""

from __future__ import annotations

import hashlib
import math
import struct
from datetime import datetime, timedelta
from decimal import Decimal

from app.data.providers.base import Candle
from app.data.timeframes import Timeframe, align_to_bucket

#: Static plausible baselines: (base_price, pip_size). Values are fixtures.
PAIR_SPECS: dict[str, tuple[float, float]] = {
    "EURUSD": (1.0850, 0.0001),
    "GBPUSD": (1.2700, 0.0001),
    "USDJPY": (155.250, 0.01),
    "AUDUSD": (0.6550, 0.0001),
    "USDCAD": (1.3700, 0.0001),
    "USDCHF": (0.8800, 0.0001),
    "NZDUSD": (0.5900, 0.0001),
}

_PIP_DECIMALS: dict[float, int] = {0.0001: 5, 0.01: 3}

_OCTAVES: tuple[tuple[int, float], ...] = ((288, 1.00), (48, 0.55), (8, 0.30), (2, 0.15))


def _hash_uniform(key: str) -> float:
    """Deterministic uniform [0, 1) from a string key."""
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    (raw,) = struct.unpack(">Q", digest)
    return float(raw) / float(1 << 64)


def _smooth(t: float) -> float:
    """Cosine interpolation weight in [0, 1]."""
    return (1.0 - math.cos(math.pi * t)) / 2.0


def _noise(pair: str, octave_period: int, x: float) -> float:
    """Value noise in [-1, 1] at continuous position ``x`` for one octave."""
    i = math.floor(x)
    frac = x - i
    a = _hash_uniform(f"forex-ai|{pair}|{octave_period}|{i}")
    b = _hash_uniform(f"forex-ai|{pair}|{octave_period}|{i + 1}")
    return (a + (b - a) * _smooth(frac)) * 2.0 - 1.0


def _price_at(pair: str, timeframe: str, ts: datetime) -> float:
    """Log-price evaluation at an instant; O(1), deterministic."""
    base_price, _pip = PAIR_SPECS.get(pair, PAIR_SPECS["EURUSD"])
    epoch = datetime(2024, 1, 1, tzinfo=ts.tzinfo)
    minutes_since = (ts - epoch).total_seconds() / 60.0
    log_price = math.log(base_price)
    # Volatility scales with timeframe so higher TFs swing more in absolute terms.
    tf_scale = {"M5": 0.35, "M15": 0.5, "H1": 0.75, "H4": 1.0, "D1": 1.35}[timeframe]
    amplitude_base = 0.0028 * tf_scale
    for period, amp_share in _OCTAVES:
        octave_amp = amplitude_base * amp_share / max(1.0, math.sqrt(period / 8))
        log_price += octave_amp * _noise(pair, period, minutes_since / period)
    return math.exp(log_price)


def _quantize(pair: str, value: float) -> Decimal:
    pip = PAIR_SPECS.get(pair, PAIR_SPECS["EURUSD"])[1]
    decimals = _PIP_DECIMALS[pip]
    return Decimal(str(round(value, decimals))).quantize(Decimal(1).scaleb(-decimals))


def generate_candle(
    symbol: str,
    timeframe: str,
    bucket_start: datetime,
) -> Candle:
    """Deterministically synthesize one closed candle.

    OHLC comes from four price samples across the bucket's lifetime; high/low
    include a small intra-bar excursion so ranges look organic.
    """
    step = timedelta(seconds=Timeframe.seconds(timeframe))
    fractions = (0.0, 1 / 3, 2 / 3, 1.0)
    samples = [
        _price_at(symbol, timeframe, bucket_start + timedelta(seconds=step.total_seconds() * f))
        for f in fractions
    ]
    open_p, close_p = samples[0], samples[-1]
    body_high, body_low = max(samples), min(samples)
    wick = abs(open_p - close_p) + (body_high - body_low)
    wick += _price_at(symbol, timeframe, bucket_start + step / 2) * 0.00008
    high = body_high + wick * 0.25
    low = body_low - wick * 0.25

    volume_noise = _hash_uniform(
        f"forex-ai|vol|{symbol}|{timeframe}|{int(bucket_start.timestamp())}"
    )
    volume = int(800 + volume_noise * 4200)

    candle = Candle(
        symbol=symbol,
        timeframe=timeframe,
        bucket_start=align_to_bucket(bucket_start, timeframe),
        open=_quantize(symbol, open_p),
        high=_quantize(symbol, high),
        low=_quantize(symbol, low),
        close=_quantize(symbol, close_p),
        volume=volume,
        complete=True,
    )
    candle.validate()
    return candle


def synthetic_candles(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> list[Candle]:
    """All closed synthetic candles with bucket start in [start, end)."""
    from app.data.timeframes import iterate_buckets, previous_closed_bucket

    closed_end = min(
        end,
        previous_closed_bucket(end, timeframe) + timedelta(seconds=Timeframe.seconds(timeframe)),
    )
    return [
        generate_candle(symbol, timeframe, bucket)
        for bucket in iterate_buckets(start, closed_end, timeframe)
        if bucket + timedelta(seconds=Timeframe.seconds(timeframe)) <= end
    ]
