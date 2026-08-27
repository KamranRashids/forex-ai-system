"""SAFE MODE regression tests: the decision pipeline is analysis/paper only.

These assert the L3 invariants that protect against any live-execution shape:
- statuses are exactly ANALYSIS / PAPER / BLOCKED (no order/live members).
- fused direction is exactly LONG / SHORT / FLAT.
- a decision can only reach PAPER when *every* risk gate passes and risk is
  enabled.
- missing ATR/price, contradicting inputs, or insufficient coverage can never
  produce a PAPER intent (they skip or degrade to ANALYSIS/BLOCKED).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.agents.base import AgentSignal, Direction
from app.data.risk_config import RiskParams
from app.decisions.engine import (
    DecisionAction,
    DecisionInputs,
    OrchParams,
    compute_decision,
)
from app.decisions.risk import GateState, RiskDeps, assess
from app.models.decision import DecisionDirection, DecisionStatus

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_BUCKET = _NOW - timedelta(hours=1)
_VALID_UNTIL = _NOW + timedelta(hours=2)


def mk(agent: str, direction: Direction, confidence: float, **feat: object) -> AgentSignal:
    sig = AgentSignal(
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
    if feat:
        sig.features.update(feat)
    return sig


def full_signals(
    regime: str = "trending", **votes: tuple[Direction, float]
) -> dict[str, AgentSignal]:
    default = {
        "technical": (Direction.LONG, 0.7),
        "fundamental": (Direction.LONG, 0.7),
        "sentiment": (Direction.LONG, 0.7),
    }
    default.update(votes)
    signals = {
        "technical": mk("technical", *default["technical"]),
        "fundamental": mk("fundamental", *default["fundamental"]),
        "sentiment": mk("sentiment", *default["sentiment"]),
    }
    signals["regime"] = mk("regime", Direction.FLAT, 0.0, regime=regime)
    return signals


def orch(**kw: float | int) -> OrchParams:
    base = dict(
        coverage_min=0.5,
        agreement_min=0.5,
        threshold=0.15,
        hysteresis=0.04,
        context_weight=0.15,
        cooldown_seconds=1800,
    )
    base.update(kw)
    return OrchParams(**base)


def risk(**kw: float | bool) -> RiskParams:
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
    base.update(kw)
    return RiskParams(**base)


def inputs(signals: dict[str, AgentSignal], *, coverage: float, **kw) -> DecisionInputs:
    fields = dict(
        symbol="EURUSD",
        timeframe="H1",
        bucket_ts=_BUCKET,
        now=_NOW,
        signals=signals,
        regime="trending",
        atr=0.005,
        price=1.10,
        coverage=coverage,
        gate=GateState(),
    )
    fields.update(kw)
    return DecisionInputs(**fields)


# ---------------------------------------------------------------------------
# L3: no live-order shape may exist.
# ---------------------------------------------------------------------------


@pytest.mark.safety
def test_decision_status_enum_has_no_live_state() -> None:
    values = set(DecisionStatus)
    assert values == {DecisionStatus.ANALYSIS, DecisionStatus.PAPER, DecisionStatus.BLOCKED}
    labels = {v.value for v in values}
    assert not any(
        token in label for label in labels for token in ("ORDER", "LIVE", "FILLED", "TRADE", "EXEC")
    )


@pytest.mark.safety
def test_decision_direction_enum_limited() -> None:
    assert set(DecisionDirection) == {
        DecisionDirection.LONG,
        DecisionDirection.SHORT,
        DecisionDirection.FLAT,
    }


@pytest.mark.safety
def test_fused_direction_is_always_enumerated() -> None:
    signals = full_signals(
        technical=(Direction.LONG, 0.9),
        fundamental=(Direction.LONG, 0.9),
        sentiment=(Direction.SHORT, 0.9),
    )
    out = compute_decision(inputs(signals, coverage=1.0), orch=orch(), risk=risk())
    if out.action == DecisionAction.PERSIST:
        assert out.fused.direction.value in {"LONG", "SHORT", "FLAT"}


# ---------------------------------------------------------------------------
# L3: PAPER requires every gate to pass (fail closed).
# ---------------------------------------------------------------------------


@pytest.mark.safety
def test_paper_requires_all_gates_pass() -> None:
    base = risk()
    signals = full_signals()
    # Mutate one field gate at a time to a failing value; PAPER must never appear.
    variants = [
        ("exposure_used_pct", 0.30),
        # Correlation blocks when triggered AND over the basket cap.
        ("correlation_used_pct", 0.15),
        ("daily_loss_used_pct", 0.03),
        ("drawdown_used_pct", 0.10),
    ]
    for attr, val in variants:
        kw = {attr: val}
        if attr == "correlation_used_pct":
            kw["correlation_triggered"] = True
        gate = GateState(**kw)
        out = compute_decision(inputs(signals, coverage=1.0, gate=gate), orch=orch(), risk=base)
        assert out.status != DecisionStatus.PAPER, attr
        assert out.status == DecisionStatus.BLOCKED


@pytest.mark.safety
def test_paper_requires_risk_disabled_false() -> None:
    signals = full_signals()
    out = compute_decision(
        inputs(signals, coverage=1.0), orch=orch(), risk=risk(risk_enabled=False)
    )
    assert out.status != DecisionStatus.PAPER


@pytest.mark.safety
def test_paper_requires_agreement_above_floor() -> None:
    signals = full_signals()
    out = compute_decision(
        inputs(signals, coverage=1.0), orch=orch(agreement_min=1.01), risk=risk()
    )
    assert out.status != DecisionStatus.PAPER


@pytest.mark.safety
def test_missing_atr_fails_closed_to_blocked() -> None:
    out = assess(
        direction=Direction.LONG,
        deps=RiskDeps(atr=None, price=1.10),
        gate=GateState(),
        params=risk(),
    )
    assert out.passed is False
    assert out.paper is False


@pytest.mark.safety
def test_missing_price_fails_closed_to_blocked() -> None:
    out = assess(
        direction=Direction.LONG,
        deps=RiskDeps(atr=0.005, price=None),
        gate=GateState(),
        params=risk(),
    )
    assert out.passed is False
    assert out.paper is False


# ---------------------------------------------------------------------------
# L3: missing/contradictory inputs can never manufacture a paper intent.
# ---------------------------------------------------------------------------


@pytest.mark.safety
def test_no_directional_votes_never_papers() -> None:
    signals = full_signals(
        technical=(Direction.FLAT, 0.0),
        fundamental=(Direction.FLAT, 0.0),
        sentiment=(Direction.FLAT, 0.0),
    )
    out = compute_decision(inputs(signals, coverage=1.0), orch=orch(), risk=risk())
    assert out.action == DecisionAction.SKIP
    assert out.status is None


@pytest.mark.safety
def test_insufficient_coverage_never_papers() -> None:
    signals = {"technical": mk("technical", Direction.LONG, 0.9)}
    signals["regime"] = mk("regime", Direction.FLAT, 0.0, regime="trending")
    out = compute_decision(inputs(signals, coverage=0.25), orch=orch(), risk=risk())
    assert out.action == DecisionAction.SKIP
    assert out.skip_reason == "insufficient_coverage"


@pytest.mark.safety
def test_cancelling_votes_flat_never_paper() -> None:
    # Balanced votes cancel to FLAT; a FLAT fused direction can never paper.
    signals = full_signals(
        technical=(Direction.LONG, 0.2),
        fundamental=(Direction.SHORT, 0.4),
        sentiment=(Direction.FLAT, 0.0),
    )
    out = compute_decision(inputs(signals, coverage=1.0), orch=orch(agreement_min=0.0), risk=risk())
    assert out.fused.direction == Direction.FLAT
    assert out.status != DecisionStatus.PAPER


# ---------------------------------------------------------------------------
# L3: every persisted decision is fully risk-gated before promotion.
# ---------------------------------------------------------------------------


@pytest.mark.safety
def test_paper_outcome_holds_passing_risk() -> None:
    signals = full_signals()
    out = compute_decision(inputs(signals, coverage=1.0), orch=orch(), risk=risk())
    assert out.status == DecisionStatus.PAPER
    assert out.risk is not None
    assert out.risk.passed is True
    assert all(g.ok for g in out.risk.gates.values())
    assert out.veto_code is None
