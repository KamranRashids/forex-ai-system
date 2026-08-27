"""Sentiment analysis agent (Phase 4).

Consumes *normalized internal* news items (injected via ``ctx.meta["news"]``,
never calls a provider/LLM directly), scores each headline with a deterministic
finance lexicon, and folds them into a time-decayed aggregate per currency and
per pair.

Decay model: each scored item contributes ``score * weight`` where ``weight``
decays exponentially with age (half-life ``_DECAY_HALF_LIFE_HOURS``). The
per-currency aggregate is the base-minus-quote differential.

SAFE MODE: analysis only; no order path.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from app.agents.base import AgentSignal, AnalysisContext, BaseAgent, Direction
from app.agents.fundamental import currencies_of
from app.llm.fallback.finance_lexicon import finance_lexicon_score

#: Half-life (hours) controlling how quickly older headlines lose influence.
_DECAY_HALF_LIFE_HOURS: float = 6.0
_CONFIDENCE_PER_ITEM: float = 0.05
_MAX_CONFIDENCE: float = 0.8


def decay_weight(
    published_utc: datetime,
    now: datetime,
    half_life_hours: float = _DECAY_HALF_LIFE_HOURS,
) -> float:
    """Exponential decay weight in (0, 1] for an item's recency."""
    age_hours = max(0.0, (now - published_utc).total_seconds() / 3600.0)
    if half_life_hours <= 0:
        return 1.0
    return math.exp(-math.log(2) * age_hours / half_life_hours)


class SentimentAgent(BaseAgent):
    id = "sentiment"
    version = "1"

    def analyze(self, ctx: AnalysisContext) -> AgentSignal:
        news: list[dict[str, Any]] = ctx.meta.get("news", [])
        base, quote = currencies_of(ctx.symbol)
        now = ctx.now or datetime.now(UTC)

        per_item = self._score_items(news, now)
        base_agg = self._currency_aggregate(per_item, base)
        quote_agg = self._currency_aggregate(per_item, quote)
        differential = base_agg - quote_agg
        weighted_count = sum(max(0.0, item["weight"]) for item in per_item)

        direction, confidence = self._decide(differential, weighted_count)
        rationale = self._rationale(differential, base_agg, quote_agg, len(per_item), now)

        return self.build_signal(
            ctx,
            direction=direction,
            confidence=confidence,
            rationale=rationale,
            features={
                "differential": round(differential, 4),
                "base_agg": round(base_agg, 4),
                "quote_agg": round(quote_agg, 4),
                "item_count": len(per_item),
                "weighted_count": round(weighted_count, 4),
            },
        )

    def _score_items(self, news: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
        scored = []
        for item in news:
            headline = item.get("headline") or ""
            published = item.get("published_utc")
            if isinstance(published, str):
                try:
                    published = datetime.fromisoformat(published)
                except ValueError:
                    published = None
            if isinstance(published, datetime) and published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            result = finance_lexicon_score(headline)
            weight = decay_weight(published, now) if isinstance(published, datetime) else 1.0
            scored.append(
                {
                    "score": result.score,
                    "weight": round(weight, 4),
                    "currency": (item.get("symbols") or ())[:1],
                    "headline": headline,
                }
            )
        return scored

    def _currency_aggregate(self, scored: list[dict[str, Any]], currency: str) -> float:
        total = 0.0
        weight_sum = 0.0
        for item in scored:
            symbols = item.get("currency")
            # Symbols is a tuple of FX pairs; a currency is "touched" if it is
            # part of any pair listed for the item.
            if not _has_currency(symbols, currency):
                continue
            w = item["weight"]
            total += item["score"] * w
            weight_sum += w
        if weight_sum == 0:
            return 0.0
        return max(-1.0, min(1.0, total / weight_sum))

    def _decide(self, differential: float, weighted_count: float) -> tuple[Direction, float]:
        if differential > 0.12:
            direction = Direction.LONG
            magnitude = min(1.0, abs(differential))
        elif differential < -0.12:
            direction = Direction.SHORT
            magnitude = min(1.0, abs(differential))
        else:
            return Direction.FLAT, 0.0
        confidence = min(
            _MAX_CONFIDENCE,
            0.15 + 0.5 * magnitude + _CONFIDENCE_PER_ITEM * weighted_count,
        )
        return direction, round(min(1.0, confidence), 4)

    def _rationale(
        self, differential: float, base: float, quote: float, count: int, now: datetime
    ) -> str:
        if count == 0:
            return "no news items within window; neutral sentiment"
        return (
            f"sentiment differential={differential:+.3f} (base={base:+.3f} vs quote={quote:+.3f}) "
            f"from {count} scored headline(s) decayed to {now.isoformat()}"
        )


def _has_currency(symbols: object, currency: str) -> bool:
    if not isinstance(symbols, (list, tuple)):
        return False
    for symbol in symbols:
        if isinstance(symbol, str) and len(symbol) == 6 and currency in (symbol[:3], symbol[3:]):
            return True
    return False
