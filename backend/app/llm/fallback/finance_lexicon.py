"""Finance-lexicon sentiment scorer (deterministic fallback).

Scores a headline into a sentiment in [-1, 1] using a small domain lexicon.
This is deliberately deterministic and free of any LLM or network dependency,
so the sentiment agent works with zero keys (implementation requirement #6).
"""

from __future__ import annotations

from dataclasses import dataclass

#: Positive/negative finance-terms with weights.
_POSITIVE: dict[str, float] = {
    "beat": 0.7,
    "beats": 0.7,
    "surge": 0.8,
    "surges": 0.8,
    "rally": 0.7,
    "rallies": 0.7,
    "gain": 0.6,
    "gains": 0.6,
    "rose": 0.6,
    "rise": 0.5,
    "strong": 0.5,
    "boost": 0.6,
    "boosted": 0.6,
    "growth": 0.5,
    "above forecast": 0.7,
    "above forecasts": 0.7,
    "hawkish": 0.5,
    "tightening": 0.4,
    "expansion": 0.5,
    "recovery": 0.5,
    "upside": 0.5,
}
_NEGATIVE: dict[str, float] = {
    "miss": -0.7,
    "misses": -0.7,
    "plunge": -0.8,
    "plunges": -0.8,
    "drop": -0.6,
    "falls": -0.6,
    "fell": -0.6,
    "weak": -0.5,
    "weakness": -0.55,
    "slump": -0.75,
    "slumps": -0.75,
    "below forecast": -0.7,
    "below forecasts": -0.7,
    "dovish": -0.5,
    "cut": -0.5,
    "cuts": -0.5,
    "recession": -0.8,
    "contraction": -0.6,
    "downside": -0.5,
    "unemployment": -0.4,
    "inflation": -0.3,
}

_INTENSIFIERS: dict[str, float] = {"sharply": 0.3, "strongly": 0.25, "significantly": 0.2}
_NEGATORS: frozenset[str] = frozenset({"not", "no", "without", "misses"})
_NEGATOR_PHRASES: frozenset[str] = frozenset({"does not", "did not", "won't", "no significant"})


@dataclass(frozen=True, slots=True)
class HeadlineScore:
    """Deterministic sentiment for one headline."""

    score: float  # in [-1, 1]
    confidence: float  # in [0, 1]
    matched_terms: tuple[str, ...]
    label: str  # positive | negative | neutral

    def normalize(self) -> HeadlineScore:
        score = max(-1.0, min(1.0, self.score))
        return HeadlineScore(score, self.confidence, self.matched_terms, _label(score))


def _label(score: float) -> str:
    if score > 0.15:
        return "positive"
    if score < -0.15:
        return "negative"
    return "neutral"


def _load_text(headline: str) -> str:
    return headline.lower() if headline else ""


def _contains_phrase(text: str, phrase: str) -> bool:
    return phrase in text


def finance_lexicon_score(headline: str) -> HeadlineScore:
    """Score a headline deterministically using the lexicon."""
    text = _load_text(headline)
    if not text:
        return HeadlineScore(0.0, 0.0, (), "neutral")
    matched: list[str] = []
    score = 0.0
    for phrase, weight in _POSITIVE.items():
        if _contains_phrase(text, phrase):
            matched.append(phrase)
            score += weight
    for phrase, weight in _NEGATIVE.items():
        if _contains_phrase(text, phrase):
            matched.append(phrase)
            score += weight
    for word, weight in _INTENSIFIERS.items():
        if _contains_phrase(text, word):
            score += weight if score >= 0 else -weight
    # Simple negation: if a negator immediately precedes a matched term,
    # flip its sign. This is a coarse heuristic kept deterministic.
    for neg in _NEGATOR_PHRASES:
        if _contains_phrase(text, neg):
            score = -score
            break
    if "not" in text.split() and score != 0.0:
        score = -score
    confidence = min(1.0, 0.3 + 0.05 * len(matched))
    return HeadlineScore(
        round(score, 4),
        round(confidence, 4),
        tuple(sorted(set(matched))),
        _label(score),
    )


def finance_lexicon_score_clean(headline: str) -> dict[str, object]:
    """Dict form convenient for feature storage."""
    result = finance_lexicon_score(headline)
    return {
        "score": result.score,
        "confidence": result.confidence,
        "label": result.label,
        "matched_terms": list(result.matched_terms),
    }
