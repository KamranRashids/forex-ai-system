"""Unit tests: risk param override bounds-clamping (fail-closed defaults)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from app.data.risk_config import RiskParams, _clamped


@pytest.mark.unit
def test_clamped_accepts_in_bounds() -> None:
    assert _clamped(0.05, (0.0, 1.0), default=0.01) == 0.05
    assert _clamped(0.0, (0.0, 1.0), default=0.01) == 0.0
    assert _clamped(1.0, (0.0, 1.0), default=0.01) == 1.0


@pytest.mark.unit
@pytest.mark.parametrize("bad", [1.5, -0.1, "abc", None, [], object()])
def test_clamped_defaults_out_of_bounds(bad: object) -> None:
    assert _clamped(bad, (0.0, 1.0), default=0.01) == 0.01


@pytest.mark.unit
def test_clamped_open_upper_bound() -> None:
    # min_rr has (0.0, None): large values are allowed.
    assert _clamped(100.0, (0.0, None), default=1.5) == 100.0
    assert _clamped(-1.0, (0.0, None), default=1.5) == 1.5


@pytest.mark.unit
def test_risk_params_frozen() -> None:
    p = RiskParams(
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
    with pytest.raises(FrozenInstanceError):
        p.max_risk_pct_account = 0.99  # type: ignore[misc]  # frozen dataclass
