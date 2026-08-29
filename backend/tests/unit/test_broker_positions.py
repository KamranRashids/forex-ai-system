"""Unit tests: paper position & PnL math (exposure source of truth)."""

from __future__ import annotations

from datetime import datetime

import pytest
from app.agents.base import Direction
from app.broker.positions import Position, PositionSet

_TS = datetime(2026, 1, 1, 12, 0)


def _pos(symbol: str, side: Direction, price: float = 1.1000, units: float = 1000.0) -> Position:
    return Position(
        symbol=symbol,
        timeframe="H1",
        units=units,
        entry_price=price,
        entry_ts=_TS,
        side=side,
    )


def test_notional():
    p = _pos("EURUSD", Direction.LONG, price=1.1000, units=10_000)
    assert p.notional == 11_000.0


def test_unrealized_long_and_short():
    long_pos = _pos("EURUSD", Direction.LONG, price=1.1000, units=1000)
    assert long_pos.unrealized(1.1050) == pytest.approx(5.0)
    assert long_pos.unrealized(1.0950) == pytest.approx(-5.0)
    short_pos = _pos("EURUSD", Direction.SHORT, price=1.1000, units=1000)
    assert short_pos.unrealized(1.0950) == pytest.approx(5.0)


def test_open_for_and_close():
    ps = PositionSet()
    ps.positions.append(_pos("EURUSD", Direction.LONG))
    assert ps.open_for("EURUSD") is not None
    assert ps.open_for("GBPUSD") is None
    assert ps.close("EURUSD") is True
    assert ps.open_for("EURUSD") is None


def test_total_notional_sums():
    ps = PositionSet()
    ps.positions.append(_pos("EURUSD", Direction.LONG, price=1.0, units=100))
    ps.positions.append(_pos("GBPUSD", Direction.SHORT, price=2.0, units=50))
    assert ps.total_notional() == 200.0


def test_correlation_shared_ccy():
    ps = PositionSet()
    ps.positions.append(_pos("EURUSD", Direction.LONG))
    # GBPUSD shares the USD leg but contrary side => correlation model only
    # flags LONG shares (the default correlation basket is long-aligned).
    assert ps.correlation_triggered("GBPUSD") is True
    assert ps.close("EURUSD")
    assert ps.correlation_triggered("GBPUSD") is False


def test_exit_price_for_long():
    p = _pos("EURUSD", Direction.LONG, price=1.1000)
    assert p.exit_price_for(1.0900, sl=1.0910, tp=None) == (1.0910, "stop_loss", True)
    assert p.exit_price_for(1.1100, sl=None, tp=1.1090) == (1.1090, "take_profit", True)
    assert p.exit_price_for(1.1050, sl=1.0900, tp=1.1200) == (1.1050, "signal", False)


def test_exit_price_for_short():
    p = _pos("EURUSD", Direction.SHORT, price=1.1000)
    assert p.exit_price_for(1.1150, sl=1.1100, tp=None) == (1.1100, "stop_loss", True)
    assert p.exit_price_for(1.0850, sl=None, tp=1.0900) == (1.0900, "take_profit", True)
