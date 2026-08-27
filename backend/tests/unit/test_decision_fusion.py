"""Unit tests: decision fusion (regime-conditional signal combination)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.agents.base import AgentSignal, Direction
from app.decisions.fusion import (
    DEFAULT_WEIGHTS,
    FusionParams,
    apply_context,
    direction_for_score,
    fuse,
    sign_of,
)

_SYMBOL = "EURUSD"
_TF = "H1"
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_VALID_UNTIL = _NOW + timedelta(hours=2)


def mk(agent: str, direction: Direction, confidence: float) -> AgentSignal:
    return AgentSignal(
        agent_id=agent,
        version="1",
        symbol=_SYMBOL,
        timeframe=_TF,
        direction=direction,
        confidence=confidence,
        bucket_ts=_NOW,
        rationale="",
        features={},
        created_at=_NOW,
        valid_until=_VALID_UNTIL,
        run_id="test",
    )


@pytest.mark.unit
def test_sign_of_maps_directions() -> None:
    assert sign_of(Direction.LONG) == 1
    assert sign_of(Direction.SHORT) == -1
    assert sign_of(Direction.FLAT) == 0


@pytest.mark.unit
def test_direction_for_score_threshold() -> None:
    assert direction_for_score(0.2, threshold=0.15) == Direction.LONG
    assert direction_for_score(-0.2, threshold=0.15) == Direction.SHORT
    assert direction_for_score(0.05, threshold=0.15) == Direction.FLAT


@pytest.mark.unit
def test_fuse_no_votes_returns_flat() -> None:
    result = fuse({"technical": mk("technical", Direction.FLAT, 0.0)}, regime="trending")
    assert result.direction == Direction.FLAT
    assert not result.has_votes
    assert result.voting_agents == ()
    assert result.weights == {}


@pytest.mark.unit
def test_fuse_missing_directional_agents_ignored() -> None:
    signals = {
        "technical": mk("technical", Direction.LONG, 0.8),
        "fundamental": mk("fundamental", Direction.FLAT, 0.0),
    }
    result = fuse(signals, regime="trending")
    assert result.has_votes
    assert result.voting_agents == ("technical",)
    assert result.weights == {"technical": 1.0}


@pytest.mark.unit
def test_fuse_unanimous_long() -> None:
    signals = {
        "technical": mk("technical", Direction.LONG, 0.8),
        "fundamental": mk("fundamental", Direction.LONG, 0.6),
        "sentiment": mk("sentiment", Direction.LONG, 0.5),
    }
    result = fuse(signals, regime="trending")
    assert result.direction == Direction.LONG
    assert result.score >= 0.55
    assert result.agreement == 1.0
    assert len(result.voting_agents) == 3
    # Weights renormalized to 1.0.
    assert abs(sum(result.weights.values()) - 1.0) < 1e-6


@pytest.mark.unit
def test_fuse_contradiction_lowers_agreement() -> None:
    signals = {
        "technical": mk("technical", Direction.LONG, 0.8),
        "sentiment": mk("sentiment", Direction.SHORT, 0.8),
    }
    result = fuse(signals, regime="trending")
    # Partial cancellation lowers agreement well below 1.0.
    assert result.agreement < 1.0
    # And it stays well below the unanimous score.
    unanimous = fuse(
        {
            "technical": mk("technical", Direction.LONG, 0.8),
            "sentiment": mk("sentiment", Direction.LONG, 0.8),
        },
        regime="trending",
    )
    assert result.score < unanimous.score


@pytest.mark.unit
def test_fuse_regime_selects_matrix() -> None:
    # In 'range' regime technical has weight 0.60; a lone technical vote keeps
    # weight 1.0 after renormalization regardless of regime.
    signals = {"technical": mk("technical", Direction.SHORT, 0.9)}
    for regime in ("trending", "range", "trending_unknown"):
        result = fuse(signals, regime=regime, params=FusionParams(weights={"a": {"x": 1.0}}))
        assert result.direction == Direction.SHORT


@pytest.mark.unit
def test_fuse_custom_params_threshold() -> None:
    signals = {"technical": mk("technical", Direction.LONG, 0.3)}
    result = fuse(signals, regime="trending", params=FusionParams(threshold=0.5))
    assert result.direction == Direction.FLAT


@pytest.mark.unit
def test_weights_renormalize_across_voters() -> None:
    weights = DEFAULT_WEIGHTS["trending"]  # technical .55 fund .20 sent .25
    signals = {
        "technical": mk("technical", Direction.LONG, 1.0),
        "fundamental": mk("fundamental", Direction.LONG, 1.0),
        "sentiment": mk("sentiment", Direction.LONG, 1.0),
    }
    result = fuse(signals, regime="trending")
    assert abs(result.weights["technical"] - weights["technical"]) < 1e-6


@pytest.mark.unit
def test_apply_context_nudges_toward_parent() -> None:
    from app.decisions.fusion import ContextInput

    base = fuse(
        {
            "technical": mk("technical", Direction.LONG, 0.1),
        },
        regime="trending",
    )
    assert base.direction == Direction.FLAT  # below threshold
    ctx = ContextInput(direction=Direction.LONG, confidence=0.9, weight=0.3)
    boosted = apply_context(base, ctx)
    assert boosted.direction == Direction.LONG
    assert boosted.pre_context_score == base.score
    assert boosted.score > base.score


@pytest.mark.unit
def test_apply_context_none_runs_unchanged() -> None:
    base = fuse(
        {
            "technical": mk("technical", Direction.LONG, 0.2),
        },
        regime="trending",
    )
    unchanged = apply_context(base, None)
    assert unchanged.score == base.score
    assert unchanged.direction == base.direction


@pytest.mark.unit
def test_default_weights_sum_to_one() -> None:
    for regime, w in DEFAULT_WEIGHTS.items():
        assert abs(sum(w.values()) - 1.0) < 1e-6, regime


@pytest.mark.unit
def test_context_for_helper() -> None:
    from app.decisions.fusion import ContextInput

    ctx = ContextInput(direction=Direction.SHORT, confidence=0.7)
    assert ctx.direction == Direction.SHORT
    assert ctx.confidence == 0.7
