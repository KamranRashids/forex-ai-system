"""Unit tests: risk agent — paper sizing + fail-closed gates (SAFE MODE)."""

from __future__ import annotations

import pytest
from app.agents.base import Direction
from app.data.risk_config import RiskParams
from app.decisions.risk import (
    GateState,
    RiskDeps,
    assess,
    compute_sizing,
)


def params(**overrides: float | bool) -> RiskParams:
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


def empty_gate(**overrides: float | bool) -> GateState:
    base = dict(
        exposure_used_pct=0.0,
        correlation_used_pct=0.0,
        daily_loss_used_pct=0.0,
        drawdown_used_pct=0.0,
        correlation_triggered=False,
    )
    base.update(overrides)
    return GateState(**base)


@pytest.mark.unit
def test_compute_sizing_atr_based() -> None:
    p = params()
    sizing = compute_sizing(price=1.1000, atr=0.0050, direction=Direction.LONG, params=p)
    assert sizing.stop_loss < 1.1000
    assert sizing.take_profit > 1.1000
    assert sizing.rr_ratio == pytest.approx(2.5 / 1.5, abs=0.0001)
    assert sizing.position_size_units > 0


@pytest.mark.unit
def test_compute_sizing_short_direction() -> None:
    p = params()
    sizing = compute_sizing(price=1.1000, atr=0.0050, direction=Direction.SHORT, params=p)
    assert sizing.stop_loss > 1.1000
    assert sizing.take_profit < 1.1000


@pytest.mark.unit
def test_assess_paper_all_gates_pass() -> None:
    outcome = assess(
        direction=Direction.LONG,
        deps=RiskDeps(atr=0.005, price=1.10),
        gate=empty_gate(),
        params=params(),
    )
    assert outcome.passed is True
    assert outcome.paper is True
    assert all(g.ok for g in outcome.gates.values())


@pytest.mark.unit
def test_assess_exposure_gate_blocks() -> None:
    outcome = assess(
        direction=Direction.LONG,
        deps=RiskDeps(atr=0.005, price=1.10),
        gate=empty_gate(exposure_used_pct=0.30),
        params=params(),  # max exposure .30, adding .01 exceeds
    )
    assert outcome.passed is False
    assert outcome.gates["exposure"].ok is False


@pytest.mark.unit
def test_assess_correlation_gate_blocks() -> None:
    outcome = assess(
        direction=Direction.LONG,
        deps=RiskDeps(atr=0.005, price=1.10),
        gate=empty_gate(correlation_triggered=True, correlation_used_pct=0.15),
        params=params(),
    )
    assert outcome.passed is False
    assert outcome.gates["correlation"].ok is False


@pytest.mark.unit
def test_assess_daily_loss_gate_blocks() -> None:
    outcome = assess(
        direction=Direction.LONG,
        deps=RiskDeps(atr=0.005, price=1.10),
        gate=empty_gate(daily_loss_used_pct=0.03),
        params=params(),
    )
    assert outcome.passed is False
    assert outcome.gates["daily_loss"].ok is False


@pytest.mark.unit
def test_assess_drawdown_gate_blocks() -> None:
    outcome = assess(
        direction=Direction.LONG,
        deps=RiskDeps(atr=0.005, price=1.10),
        gate=empty_gate(drawdown_used_pct=0.10),
        params=params(),
    )
    assert outcome.passed is False
    assert outcome.gates["drawdown"].ok is False


@pytest.mark.unit
def test_assess_rr_gate_blocks() -> None:
    outcome = assess(
        direction=Direction.LONG,
        deps=RiskDeps(atr=0.005, price=1.10),
        gate=empty_gate(),
        params=params(min_rr=10.0),  # impossible RR
    )
    assert outcome.passed is False
    assert outcome.gates["rr"].ok is False


@pytest.mark.unit
def test_assess_fail_closed_missing_atr() -> None:
    outcome = assess(
        direction=Direction.LONG,
        deps=RiskDeps(atr=None, price=1.10),
        gate=empty_gate(),
        params=params(),
    )
    assert outcome.passed is False
    assert outcome.paper is False
    assert outcome.gates["sizing"].ok is False


@pytest.mark.unit
def test_assess_fail_closed_missing_price() -> None:
    outcome = assess(
        direction=Direction.LONG,
        deps=RiskDeps(atr=0.005, price=None),
        gate=empty_gate(),
        params=params(),
    )
    assert outcome.passed is False
    assert outcome.paper is False


@pytest.mark.unit
def test_assess_flat_no_paper_even_when_passing() -> None:
    outcome = assess(
        direction=Direction.FLAT,
        deps=RiskDeps(atr=0.005, price=1.10),
        gate=empty_gate(),
        params=params(),
    )
    assert outcome.passed is True
    assert outcome.paper is False
    assert outcome.sizing is None


@pytest.mark.unit
def test_assess_risk_disabled_blocks_paper() -> None:
    outcome = assess(
        direction=Direction.LONG,
        deps=RiskDeps(atr=0.005, price=1.10),
        gate=empty_gate(),
        params=params(risk_enabled=False),
    )
    assert outcome.passed is True
    assert outcome.paper is False


@pytest.mark.unit
def test_assess_reasons_structured() -> None:
    outcome = assess(
        direction=Direction.LONG,
        deps=RiskDeps(atr=0.005, price=1.10),
        gate=empty_gate(exposure_used_pct=0.30),
        params=params(),
    )
    reasons = outcome.reasons
    codes = {r["code"] for r in reasons}
    assert "exposure" in codes
    assert all(isinstance(r["ok"], bool) for r in reasons)


@pytest.mark.unit
def test_assess_exposure_not_exceeded_when_room() -> None:
    outcome = assess(
        direction=Direction.LONG,
        deps=RiskDeps(atr=0.005, price=1.10),
        gate=empty_gate(exposure_used_pct=0.20),
        params=params(),  # .20 + .01 <= .30
    )
    assert outcome.gates["exposure"].ok is True
    assert outcome.passed is True


@pytest.mark.unit
def test_correlation_not_triggered_no_basket_cost() -> None:
    outcome = assess(
        direction=Direction.LONG,
        deps=RiskDeps(atr=0.005, price=1.10),
        gate=empty_gate(correlation_used_pct=0.99, correlation_triggered=False),
        params=params(),
    )
    assert outcome.gates["correlation"].ok is True
