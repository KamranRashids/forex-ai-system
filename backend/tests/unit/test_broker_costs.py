"""Unit tests: deterministic cost model (spread + slippage)."""

from __future__ import annotations

import pytest
from app.broker.costs import CostParams, cost_per_unit, slippage_bps


def test_slippage_is_deterministic():
    a = slippage_bps(seed=7, symbol="EURUSD", side="long", notional=100_000.0)
    b = slippage_bps(seed=7, symbol="EURUSD", side="long", notional=100_000.0)
    assert a == b


def test_slippage_changes_with_seed():
    a = slippage_bps(seed=1, symbol="EURUSD", side="long", notional=100_000.0)
    b = slippage_bps(seed=2, symbol="EURUSD", side="long", notional=100_000.0)
    assert a != b


def test_slippage_nonnegative():
    slip = slippage_bps(seed=0, symbol="GBPJPY", side="short", notional=50_000.0)
    assert slip >= 0.0


def test_cost_per_unit_includes_half_spread():
    p = CostParams(spread=0.0002, slippage_pct=0.0, commission_per_side=0.0)
    cost = cost_per_unit(
        price=1.1000, seed=0, symbol="EURUSD", side="long", notional=100_000.0, params=p
    )
    # With slippage disabled, cost is exactly half-spread (approx).
    assert cost == pytest.approx(0.0001, abs=1e-9)


def test_slippage_zero_disables_slip():
    p = CostParams(spread=0.0, slippage_pct=0.0, commission_per_side=0.0)
    assert cost_per_unit(
        price=1.0, seed=5, symbol="EURUSD", side="long", notional=50_000.0, params=p
    ) == pytest.approx(0.0, abs=1e-12)


def test_cost_total_increases_with_notional():
    p = CostParams(spread=0.0, slippage_pct=0.00002)
    small = (
        cost_per_unit(price=1.0, seed=3, symbol="EURUSD", side="long", notional=1.0, params=p) * 1.0
    )
    big = (
        cost_per_unit(
            price=1.0, seed=3, symbol="EURUSD", side="long", notional=1_000_000.0, params=p
        )
        * 1_000_000.0
    )
    assert big > small  # total adverse cost grows with size


def test_default_params_are_safe():
    p = CostParams()
    assert p.spread >= 0.0
    assert p.slippage_pct >= 0.0
    assert p.commission_per_side >= 0.0
