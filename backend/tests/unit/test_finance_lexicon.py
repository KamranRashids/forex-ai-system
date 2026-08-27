"""Unit tests for the deterministic finance-lexicon sentiment scorer (Phase 4)."""

from __future__ import annotations

import pytest
from app.llm.fallback.finance_lexicon import (
    finance_lexicon_score,
    finance_lexicon_score_clean,
)

pytestmark = [pytest.mark.unit]


def test_positive_headline_scores_positive() -> None:
    result = finance_lexicon_score("EURUSD surges after strong growth data")
    assert result.score > 0
    assert result.label == "positive"
    assert 0.0 <= result.confidence <= 1.0


def test_negative_headline_scores_negative() -> None:
    result = finance_lexicon_score("USD plummets as recession fears mount")
    assert result.score < 0
    assert result.label == "negative"


def test_empty_headline_is_neutral_zero() -> None:
    result = finance_lexicon_score("")
    assert result.score == 0.0
    assert result.confidence == 0.0
    assert result.label == "neutral"


def test_neutral_headline_stays_neutral() -> None:
    result = finance_lexicon_score("Markets eye upcoming data releases this week")
    assert result.label in ("positive", "negative", "neutral")


def test_negation_flips_sign() -> None:
    # A positive headline negated by a negator phrase flips to negative.
    result = finance_lexicon_score("company reports no significant beat this quarter")
    assert result.score < 0.0


def test_raw_score_can_exceed_unit_interval() -> None:
    # Stacked positive terms accumulate (>1); normalize() clamps to [-1, 1].
    result = finance_lexicon_score(
        " ".join(["surge", "rally", "beat", "growth", "upside", "strong", "recovery", "expansion"])
    )
    assert result.score > 1.0
    assert result.score >= 0


def test_normalize_clamps_to_unit_interval() -> None:
    result = finance_lexicon_score("surge rally beat growth upside strong recovery expansion")
    normalized = result.normalize()
    assert -1.0 <= normalized.score <= 1.0
    assert normalized.score == 1.0


def test_clean_shape_for_features() -> None:
    clean = finance_lexicon_score_clean("strong rally")
    assert set(clean) == {"score", "confidence", "label", "matched_terms"}
    assert isinstance(clean["matched_terms"], list)


def test_deterministic_across_calls() -> None:
    assert finance_lexicon_score("prices fall sharply") == finance_lexicon_score(
        "prices fall sharply"
    )


def test_matched_terms_are_sorted_and_deduplicated() -> None:
    result = finance_lexicon_score("boom rally rally")
    terms = result.matched_terms
    assert terms == tuple(sorted(set(terms)))
