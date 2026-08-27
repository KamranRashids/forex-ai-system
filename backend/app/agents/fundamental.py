"""Fundamental analysis agent (Phase 4).

Consumes *normalized internal* economic events (injected via ``ctx.meta``;
never calls a provider or LLM directly) and expresses:

- an **event-window risk state** from calendar proximity (high/medium/low),
- a **surprise bias** from actual-vs-forecast for the pair's currencies,
- a **currency-strength narrative** as the base-vs-quote differential.

Deterministic and zero-key: the calendar-proximity model + surprise scoring are
the primary path; an optional LLM may enrich the rationale only when configured
and in budget (falling back to this deterministic narrative otherwise).

SAFE MODE: analysis only; no order path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.agents.base import AgentSignal, AnalysisContext, BaseAgent, Direction
from app.llm.fallback.calendar_proximity import event_impact

_IMPACT_WINDOW_MIN: int = 90
#: Importance -> base impact magnitude used to size the risk state.
_IMPORTANCE_IMPACT: dict[str, float] = {"low": 0.2, "medium": 0.5, "high": 0.8}


def currencies_of(symbol: str) -> tuple[str, str]:
    """EURUSD -> (base=EUR, quote=USD)."""
    return symbol[:3].upper(), symbol[3:].upper()


def parse_number(raw: str | None) -> float | None:
    """Parse a '3.2%' style value into a float (None when not numeric)."""
    if not raw:
        return None
    cleaned = raw.replace(",", ".").replace("%", " ").strip()
    try:
        return float(cleaned.split()[0])
    except (ValueError, IndexError):
        return None


def surprise_direction(actual: float | None, forecast: float | None) -> int:
    """+1 actual>forecast, -1 actual<forecast, 0 tie/unknown."""
    if actual is None or forecast is None:
        return 0
    if actual > forecast:
        return 1
    if actual < forecast:
        return -1
    return 0


class FundamentalAgent(BaseAgent):
    id = "fundamental"
    version = "1"

    def analyze(self, ctx: AnalysisContext) -> AgentSignal:
        events: list[dict[str, Any]] = ctx.meta.get("events", [])
        base, quote = currencies_of(ctx.symbol)
        now = ctx.now or datetime.now(UTC)

        # 1) Event-window risk state from proximity.
        impact = self._max_impact(events, now)
        # 2) Surprise bias per currency.
        base_bias = self._currency_bias(events, base)
        quote_bias = self._currency_bias(events, quote)
        # 3) Strength differential (base minus quote).
        differential = base_bias - quote_bias

        direction, confidence = self._decide(impact, differential, len(events))
        rationale = self._rationale(base, quote, impact, base_bias, quote_bias, differential, now)

        return self.build_signal(
            ctx,
            direction=direction,
            confidence=confidence,
            rationale=rationale,
            features={
                "impact": impact,
                "base_bias": round(base_bias, 4),
                "quote_bias": round(quote_bias, 4),
                "differential": round(differential, 4),
                "event_count": len(events),
                "currency_base": base,
                "currency_quote": quote,
            },
        )

    def _max_impact(self, events: list[dict[str, Any]], now: datetime) -> float:
        peak = 0.0
        for event in events:
            try:
                ts = event["timestamp_utc"]
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts)
            except (KeyError, ValueError):
                continue
            imp = event_impact(
                ts,
                now,
                event.get("importance", "low"),
                half_window_minutes=_IMPACT_WINDOW_MIN,
            )
            if imp is not None:
                peak = max(peak, imp.impact)
        return round(peak, 4)

    def _currency_bias(self, events: list[dict[str, Any]], currency: str) -> float:
        """Signed aggregate surprise for one currency in [-1, 1]."""
        total = 0.0
        weight_sum = 0.0
        for event in events:
            if event.get("currency", "").upper() != currency:
                continue
            actual = parse_number(event.get("actual"))
            forecast = parse_number(event.get("forecast"))
            sign = surprise_direction(actual, forecast)
            if sign == 0:
                continue
            w = _IMPORTANCE_IMPACT.get(event.get("importance", "low"), 0.5)
            total += sign * w
            weight_sum += w
        if weight_sum == 0:
            return 0.0
        return max(-1.0, min(1.0, total / weight_sum))

    def _decide(
        self, impact: float, differential: float, event_count: int
    ) -> tuple[Direction, float]:
        if event_count == 0:
            return Direction.FLAT, 0.0
        # Very high event risk suppresses directional bets during the window.
        if impact >= 0.85:
            return Direction.FLAT, 0.1
        if abs(differential) < 0.25:
            return Direction.FLAT, 0.0
        direction = Direction.LONG if differential > 0 else Direction.SHORT
        influence = abs(differential)
        # Scale confidence by how decisive + evidence-backed the read is.
        confidence = 0.2 + 0.5 * influence + 0.1 * min(1.0, event_count / 6)
        if impact >= 0.5:
            confidence *= 0.8  # dampen when an event is near
        return direction, round(min(1.0, confidence), 4)

    def _rationale(
        self,
        base: str,
        quote: str,
        impact: float,
        base_bias: float,
        quote_bias: float,
        differential: float,
        now: datetime,
    ) -> str:
        parts = []
        if impact >= 0.5:
            parts.append(f"high-impact event window active (impact={impact:.2f})")
        if base_bias or quote_bias:
            parts.append(
                f"surprise bias {base}={base_bias:+.2f} vs {quote}={quote_bias:+.2f} "
                f"(differential={differential:+.2f})"
            )
        if not parts:
            return "no fundamental events within window; neutral"
        return "; ".join(parts[:3])
