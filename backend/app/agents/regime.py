"""Regime classification agent (Phase 3 conditioning metadata).

Labels the current market regime for downstream orchestration:

- ``regime``: trending | weakening_trend | transitional | range
  from ADX(14) level and 4-bar slope:
      adx >= 25 and slope >= 0 -> trending
      adx >= 25 and slope <  0 -> weakening_trend
      20 <= adx < 25           -> transitional
      adx < 20                 -> range
- ``volatility``: ATR% (ATR14/close*100) percentile within the trailing
  window (max 100 bars): terciles -> low | normal | high.
- ``session``: UTC-hour FX session bucket:
      asia 22-06 | london 07-11 | overlap 12-15 | newyork 16-20 | late 21.

Output contract: direction is always FLAT and confidence 0.0 — this agent
emits conditioning metadata, not trade direction. Freshness multiplier is 4x
the timeframe interval (see base.valid_until_for).
"""

from __future__ import annotations

from datetime import UTC

from app.agents.base import AgentSignal, AnalysisContext, BaseAgent, Direction
from app.agents.indicators import adx as adx_indicator
from app.agents.indicators import atr, last_valid

_MIN_BARS: int = 40
_SLOPE_LOOKBACK: int = 4
_PERCENTILE_WINDOW: int = 100


class RegimeAgent(BaseAgent):
    id = "regime"
    version = "1"

    def analyze(self, ctx: AnalysisContext) -> AgentSignal:
        data = ctx.candles
        if len(data) < _MIN_BARS:
            return self.build_signal(
                ctx,
                direction=Direction.FLAT,
                confidence=0.0,
                rationale=f"insufficient_history ({len(data)} < {_MIN_BARS} bars)",
                features={"regime": "unknown", "volatility": "unknown", "session": "unknown"},
            )

        close = data["close"]
        adx_line, plus_di, minus_di = adx_indicator(data, length=14)
        atr14 = atr(data, length=14)

        f_adx = last_valid(adx_line)
        f_atr = last_valid(atr14)
        f_close = last_valid(close)
        if f_adx is None or f_atr is None or f_close is None or f_close == 0:
            return self._unknown(ctx)

        slope_index = max(0, len(adx_line.dropna()) - 1 - _SLOPE_LOOKBACK)
        dropped = adx_line.dropna()
        slope_base = float(dropped.iloc[slope_index]) if len(dropped) > slope_index else f_adx
        slope = f_adx - slope_base

        atr_pct_series = (atr14 / close * 100.0).dropna()
        trailing = atr_pct_series.tail(_PERCENTILE_WINDOW)
        f_atr_pct = float(trailing.iloc[-1])
        percentile = float((trailing < f_atr_pct).mean()) if len(trailing) else 0.5

        features: dict[str, object] = {
            "regime": classify_regime(f_adx, slope),
            "volatility": volatility_bucket(percentile),
            "session": session_bucket(ctx.now.astimezone(UTC).hour),
            "adx": round(f_adx, 4),
            "adx_slope_4bar": round(slope, 4),
            "plus_di": round(last_valid(plus_di) or 0.0, 4),
            "minus_di": round(last_valid(minus_di) or 0.0, 4),
            "atr_pct": round(f_atr_pct, 4),
            "atr_percentile": round(percentile, 4),
        }
        rationale = (
            f"regime={features['regime']} vol={features['volatility']} "
            f"session={features['session']} (adx={f_adx:.1f}, "
            f"slope={slope:+.1f}, atrpct={f_atr_pct:.2f}@p{percentile:.0f})"
        )
        return self.build_signal(
            ctx,
            direction=Direction.FLAT,
            confidence=0.0,
            rationale=rationale,
            features=features,
        )

    def _unknown(self, ctx: AnalysisContext) -> AgentSignal:
        return self.build_signal(
            ctx,
            direction=Direction.FLAT,
            confidence=0.0,
            rationale="indicator inputs unavailable",
            features={"regime": "unknown", "volatility": "unknown", "session": "unknown"},
        )


def classify_regime(adx_value: float, slope: float) -> str:
    """ADX threshold/slope classifier (documented boundaries)."""
    if adx_value >= 25:
        return "trending" if slope >= 0 else "weakening_trend"
    if adx_value < 20:
        return "range"
    return "transitional"


def volatility_bucket(percentile: float) -> str:
    """Tercile bucket of the ATR% percentile rank."""
    if percentile < 1 / 3:
        return "low"
    if percentile < 2 / 3:
        return "normal"
    return "high"


def session_bucket(hour_utc: int) -> str:
    """FX session buckets by UTC hour."""
    if hour_utc >= 22 or hour_utc <= 6:
        return "asia"
    if hour_utc <= 11:
        return "london"
    if hour_utc <= 15:
        return "overlap"
    if hour_utc <= 20:
        return "newyork"
    return "late"
