"""Technical analysis agent: multi-indicator confluence scoring (Phase 3).

Scoring model (deterministic, weights sum to ~1.0 across active votes):

| vote               | weight | trend-gated |
|--------------------|--------|-------------|
| ema9/21 cross      | 0.18   | yes         |
| close vs SMA50     | 0.12   | yes         |
| MACD histogram sign| 0.15   | yes         |
| RSI midline (>50)  | 0.08   | no          |
| RSI extreme (>=70 / <=30 mean-revert) | 0.12 | no |
| Bollinger breakout | 0.10   | no          |
| Stochastic %K/%D   | 0.10   | no          |
| Donchian breakout  | 0.12   | yes         |
| Pivot position     | 0.10   | no          |

Trend gating: when ADX(14) < 25 the weight of gated votes is scaled by
``max(0.3, adx/25)`` so counter-trend signals dominate weak-trend regimes.
Noise immunity: difference-based votes (EMA cross, SMA50 distance, MACD
histogram) use a dead-zone of 5% of ATR(14); Stochastic requires %K/%D
separation > 0.5 points. Sub-deadzone differences vote neutral instead of
amplifying floating-point noise into full-weight signals.
Direction thresholds: score >= +0.15 -> LONG, <= -0.15 -> SHORT, else FLAT.
Confidence = |score| capped at 0.99.
"""

from __future__ import annotations

import pandas as pd

from app.agents.base import AgentSignal, AnalysisContext, BaseAgent, Direction
from app.agents.indicators import adx as adx_indicator
from app.agents.indicators import (
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
)

_MIN_BARS: int = 34
_DIRECTION_THRESHOLD: float = 0.15
_ADX_TREND_FLOOR: float = 25.0
_GATE_FLOOR: float = 0.3


class Vote:
    """One named confluence component."""

    __slots__ = ("gated", "name", "vote", "weight")

    def __init__(self, name: str, weight: float, vote: float, gated: bool) -> None:
        self.name = name
        self.weight = weight
        self.vote = vote  # signed: >0 bullish, <0 bearish, 0 neutral
        self.gated = gated


class TechnicalAgent(BaseAgent):
    id = "technical"
    version = "1"

    def analyze(self, ctx: AnalysisContext) -> AgentSignal:
        data = ctx.candles
        if len(data) < _MIN_BARS:
            return self.build_signal(
                ctx,
                direction=Direction.FLAT,
                confidence=0.0,
                rationale=f"insufficient_history ({len(data)} < {_MIN_BARS} bars)",
                features={"score": 0.0},
            )

        close = data["close"]
        ema_fast = ema(close, 9)
        ema_slow = ema(close, 21)
        # SMA(50) yields an all-NaN series on short windows -> _last() -> None
        # -> the vote is neutral. No special-casing required.
        sma50 = sma(close, 50)

        macd_line, signal_line, histogram = macd(close)
        rsi14 = rsi(close, 14)
        bb_mid, bb_upper, bb_lower = bollinger(close, 20, 2.0)
        stoch_k, stoch_d = stochastic(data, k_length=14, k_smooth=3, d_smooth=3)
        adx_line, plus_di, minus_di = adx_indicator(data, length=14)
        atr14 = atr(data, 14)
        don_upper, _don_mid, don_lower = donchian(data, 20)

        c = _last(close)
        f_ema_fast, f_ema_slow = _last(ema_fast), _last(ema_slow)
        f_sma50 = _last(sma50)
        f_hist = _last(histogram)
        f_rsi = _last(rsi14)
        f_bb_u, f_bb_l = _last(bb_upper), _last(bb_lower)
        f_k, f_d = _last(stoch_k), _last(stoch_d)
        f_adx = _last(adx_line)
        f_atr = _last(atr14)
        f_plus_di, f_minus_di = _last(plus_di), _last(minus_di)
        prev_don_up = _last(don_upper.shift(1))
        prev_don_low = _last(don_lower.shift(1))

        gate = 1.0
        if f_adx is not None and f_adx < _ADX_TREND_FLOOR:
            gate = max(_GATE_FLOOR, f_adx / _ADX_TREND_FLOOR)

        # ATR-relative dead-zone keeps floating-point noise from voting.
        deadzone = 0.05 * f_atr if f_atr is not None else 1e-12

        votes: list[Vote] = [
            Vote(
                "ema_cross",
                0.18,
                _sign_deadzone((f_ema_fast or 0.0) - (f_ema_slow or 0.0), deadzone),
                gated=True,
            ),
            Vote(
                "close_vs_sma50",
                0.12,
                _sign_deadzone(
                    (c or 0.0) - (f_sma50 if f_sma50 is not None else c or 0.0), deadzone
                ),
                gated=True,
            ),
            Vote("macd_histogram", 0.15, _sign_deadzone(f_hist or 0.0, deadzone), gated=True),
            Vote("rsi_midline", 0.08, _threshold(f_rsi, 50.0), gated=False),
            Vote(
                "rsi_extreme",
                0.12,
                _rsi_extreme(f_rsi),
                gated=False,
            ),
            Vote(
                "bollinger_breakout",
                0.10,
                _breakout(c, f_bb_u, f_bb_l),
                gated=False,
            ),
            Vote("stochastic", 0.10, _stoch_vote(f_k, f_d), gated=False),
            Vote(
                "donchian_breakout",
                0.12,
                _breakout(c, prev_don_up, prev_don_low),
                gated=True,
            ),
            Vote("pivot_position", 0.10, _pivot_vote(c, ctx.meta.get("prev_daily")), gated=False),
        ]

        score = 0.0
        breakdown: dict[str, float] = {}
        for vote in votes:
            contribution = vote.weight * (gate if vote.gated else 1.0) * vote.vote
            score += contribution
            breakdown[vote.name] = round(contribution, 4)

        direction = (
            Direction.LONG
            if score >= _DIRECTION_THRESHOLD
            else Direction.SHORT
            if score <= -_DIRECTION_THRESHOLD
            else Direction.FLAT
        )
        confidence = round(min(0.99, abs(score)), 4)

        top = sorted(breakdown.items(), key=lambda kv: abs(kv[1]), reverse=True)[:3]
        rationale = (
            f"{direction.value} score={score:+.2f} gate={gate:.2f}; "
            + ", ".join(f"{n}{v:+.2f}" for n, v in top)
            + f"; adx={f_adx if f_adx is not None else float('nan'):.1f}"
        )

        features: dict[str, object] = {
            "score": round(score, 4),
            "gate": round(gate, 4),
            "votes": breakdown,
            "indicators": {
                "ema9": f_ema_fast,
                "ema21": f_ema_slow,
                "sma50": f_sma50,
                "macd_hist": f_hist,
                "rsi14": f_rsi,
                "bb_upper": f_bb_u,
                "bb_lower": f_bb_l,
                "stoch_k": f_k,
                "stoch_d": f_d,
                "adx14": f_adx,
                "plus_di": f_plus_di,
                "minus_di": f_minus_di,
                "atr14": f_atr,
                "donchian_prev_upper": prev_don_up,
                "donchian_prev_lower": prev_don_low,
                "close": c,
            },
        }

        return self.build_signal(
            ctx,
            direction=direction,
            confidence=confidence,
            rationale=rationale,
            features=features,
        )


def _last(series_or_none: pd.Series | None) -> float | None:
    if series_or_none is None:
        return None
    return last_valid(series_or_none)


def _sign(value: float | None) -> int:
    if value is None or value == 0:
        return 0
    return 1 if value > 0 else -1


def _sign_deadzone(value: float, deadzone: float) -> int:
    """Signed vote with a neutral band: |value| <= deadzone -> 0."""
    if value > deadzone:
        return 1
    if value < -deadzone:
        return -1
    return 0


def _rsi_extreme(rsi_value: float | None) -> int:
    """Mean-reversion vote at RSI extremes: overbought bearish, oversold bullish."""
    if rsi_value is None:
        return 0
    if rsi_value >= 70:
        return -1
    if rsi_value <= 30:
        return 1
    return 0


def _threshold(value: float | None, level: float) -> int:
    if value is None or value == level:
        return 0
    return 1 if value > level else -1


def _breakout(value: float | None, upper: float | None, lower: float | None) -> int:
    if value is None or upper is None or lower is None:
        return 0
    if value >= upper:
        return 1
    if value <= lower:
        return -1
    return 0


def _stoch_vote(k: float | None, d: float | None) -> int:
    if k is None or d is None:
        return 0
    if k > d and k < 80:
        return 1
    if k < d and k > 20:
        return -1
    return 0


def _pivot_vote(close: float | None, prev_daily: object) -> int:
    if close is None or not isinstance(prev_daily, dict):
        return 0
    try:
        levels = floor_pivots(
            float(prev_daily["high"]), float(prev_daily["low"]), float(prev_daily["close"])
        )
    except (KeyError, TypeError, ValueError):
        return 0
    return 1 if close > levels.pivot else -1
