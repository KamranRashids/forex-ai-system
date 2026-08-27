"""Unit tests: compute_decision (pure fuse -> risk-gate -> status logic)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.agents.base import AgentSignal, Direction
from app.data.risk_config import RiskParams
from app.decisions.engine import (
    DecisionAction,
    DecisionInputs,
    OrchParams,
    SkipReason,
    compute_decision,
)
from app.decisions.risk import GateState
from app.models.decision import DecisionStatus

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_BUCKET = _NOW - timedelta(hours=1)
_VALID_UNTIL = _NOW + timedelta(hours=2)


def mk(agent: str, direction: Direction, confidence: float) -> AgentSignal:
    return AgentSignal(
        agent_id=agent,
        version="1",
        symbol="EURUSD",
        timeframe="H1",
        direction=direction,
        confidence=confidence,
        bucket_ts=_BUCKET,
        rationale="",
        features={},
        created_at=_NOW,
        valid_until=_VALID_UNTIL,
        run_id="t",
    )


def full_signals(
    *,
    tech: tuple[Direction, float] = (Direction.LONG, 0.7),
    fund: tuple[Direction, float] = (Direction.LONG, 0.7),
    sent: tuple[Direction, float] = (Direction.LONG, 0.7),
    regime: str = "trending",
) -> tuple[dict[str, AgentSignal], str]:
    signals = {
        "technical": mk("technical", *tech),
        "fundamental": mk("fundamental", *fund),
        "sentiment": mk("sentiment", *sent),
        "regime": mk("regime", Direction.FLAT, 0.0),
    }
    if regime != "trending":
        signals["regime"] = _mk_regime(regime)
    return signals, regime


def _mk_regime(regime: str) -> AgentSignal:
    sig = mk("regime", Direction.FLAT, 0.0)
    sig.features["regime"] = regime
    return sig


def orch(**overrides: float | int) -> OrchParams:
    base = dict(
        coverage_min=0.5,
        agreement_min=0.5,
        threshold=0.15,
        hysteresis=0.04,
        context_weight=0.15,
        cooldown_seconds=1800,
    )
    base.update(overrides)
    return OrchParams(**base)


def risk(**overrides: float | bool) -> RiskParams:
    base = dict(
        max_risk_pct_account=0.01,
        max_exposure_pct=0.30,
        max_daily_loss_pct=0.03,
        max_drawdown_pct=0.10,
        min_rr=1.5,
        sl_atr_multiple=1.5,
        tp_atr_multiple=2.5,
        vol_target_pct=0.20,
        correlation_cap_pct=0.15,
        risk_enabled=True,
        paper_equity=100_000.0,
    )
    base.update(overrides)
    return RiskParams(**base)


def empty_gate() -> GateState:
    return GateState()


def inputs(
    signals: dict[str, AgentSignal], regime: str, *, coverage: float, **kw
) -> DecisionInputs:
    fields = dict(
        symbol="EURUSD",
        timeframe="H1",
        bucket_ts=_BUCKET,
        now=_NOW,
        signals=signals,
        regime=regime,
        atr=0.005,
        price=1.10,
        coverage=coverage,
        gate=empty_gate(),
    )
    fields.update(kw)
    return DecisionInputs(**fields)


@pytest.mark.unit
def test_no_votes_skips() -> None:
    signals, regime = full_signals(
        tech=(Direction.FLAT, 0.0), fund=(Direction.FLAT, 0.0), sent=(Direction.FLAT, 0.0)
    )
    out = compute_decision(inputs(signals, regime, coverage=1.0), orch=orch(), risk=risk())
    assert out.action == DecisionAction.SKIP
    assert out.skip_reason == SkipReason.NO_VOTES


@pytest.mark.unit
def test_insufficient_coverage_skips() -> None:
    # Only technical + regime present -> coverage 0.5 (== min, not below).
    signals = {
        "technical": mk("technical", Direction.LONG, 0.7),
        "regime": _mk_regime("trending"),
    }
    out = compute_decision(inputs(signals, "trending", coverage=0.25), orch=orch(), risk=risk())
    assert out.action == DecisionAction.SKIP
    assert out.skip_reason == SkipReason.INSUFFICIENT_COVERAGE


@pytest.mark.unit
def test_weak_flat_skips() -> None:
    # Conflicting directional votes cancel to FLAT despite full coverage.
    signals, regime = full_signals(
        tech=(Direction.LONG, 0.2), fund=(Direction.SHORT, 0.2), sent=(Direction.LONG, 0.05)
    )
    out = compute_decision(inputs(signals, regime, coverage=1.0), orch=orch(), risk=risk())
    assert out.action == DecisionAction.SKIP
    assert out.skip_reason == SkipReason.WEAK_FLAT


@pytest.mark.unit
def test_paper_when_all_gates_pass() -> None:
    signals, regime = full_signals()
    out = compute_decision(inputs(signals, regime, coverage=1.0), orch=orch(), risk=risk())
    assert out.action == DecisionAction.PERSIST
    assert out.status == DecisionStatus.PAPER
    assert out.risk is not None and out.risk.passed


@pytest.mark.unit
def test_low_agreement_is_analysis_not_paper() -> None:
    # Force agreement below the agreement floor with an impossible requirement.
    signals, regime = full_signals()
    out = compute_decision(
        inputs(signals, regime, coverage=1.0), orch=orch(agreement_min=1.01), risk=risk()
    )
    assert out.action == DecisionAction.PERSIST
    assert out.status == DecisionStatus.ANALYSIS
    assert out.veto_code == "low_agreement"


@pytest.mark.unit
def test_risk_block_is_blocked_with_veto() -> None:
    signals, regime = full_signals()
    gate = GateState(exposure_used_pct=0.30)
    out = compute_decision(
        inputs(signals, regime, coverage=1.0, gate=gate), orch=orch(), risk=risk()
    )
    assert out.action == DecisionAction.PERSIST
    assert out.status == DecisionStatus.BLOCKED
    assert out.veto_code == "exposure"


@pytest.mark.unit
def test_parent_context_can_promote() -> None:
    # A sub-threshold base vote that a parent LONG context nudges to LONG.
    signals, regime = full_signals(
        tech=(Direction.LONG, 0.2), fund=(Direction.LONG, 0.2), sent=(Direction.LONG, 0.2)
    )

    from app.decisions.fusion import ContextInput

    ctx = inputs(
        signals,
        regime,
        coverage=1.0,
        parent_context=ContextInput(direction=Direction.LONG, confidence=0.9, weight=0.3),
    )
    out = compute_decision(ctx, orch=orch(threshold=0.2), risk=risk())
    assert out.fused.direction == Direction.LONG


@pytest.mark.unit
def test_risk_disabled_yields_analysis() -> None:
    signals, regime = full_signals()
    out = compute_decision(
        inputs(signals, regime, coverage=1.0), orch=orch(), risk=risk(risk_enabled=False)
    )
    assert out.action == DecisionAction.PERSIST
    assert out.status == DecisionStatus.ANALYSIS
    assert out.veto_code == "risk_disabled"
