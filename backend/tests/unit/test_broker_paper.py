"""Unit tests: deterministic paper broker (fills, costs, SL/TP, equity)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.base import Direction
from app.broker.costs import CostParams
from app.broker.paper import CASH_START, EXIT_SIGNAL, EXIT_SL, EXIT_TP, PaperBroker, Trade

_B1 = datetime(2026, 1, 1, tzinfo=UTC)


def _broker() -> PaperBroker:
    return PaperBroker(start_equity=100_000.0, seed=0, cost_params=CostParams())


def test_enter_and_total_equity_unchanged_at_entry():
    b = _broker()
    b.enter_at_next_open(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.1000,
        ts=_B1,
        units=1000,
    )
    pos = b.positions.open_for("EURUSD")
    assert pos is not None
    assert pos.units == 1000
    assert pos.entry_price == 1.1000
    # Mark at same price; equity should reflect only entry costs.
    b.mark_position("EURUSD", 1.1000)
    assert b.total_equity() < b.start_equity  # entry cost deducted


def test_no_double_open_for_same_symbol():
    b = _broker()
    b.enter_at_next_open(
        symbol="EURUSD", timeframe="H1", direction=Direction.LONG, ref_price=1.1, ts=_B1, units=1000
    )
    b.enter_at_next_open(
        symbol="EURUSD", timeframe="H1", direction=Direction.LONG, ref_price=1.1, ts=_B1, units=1000
    )
    assert len(b.positions.positions) == 1


def test_flat_or_zero_units_noop():
    b = _broker()
    b.enter_at_next_open(
        symbol="EURUSD", timeframe="H1", direction=Direction.FLAT, ref_price=1.1, ts=_B1, units=1000
    )
    b.enter_at_next_open(
        symbol="EURUSD", timeframe="H1", direction=Direction.LONG, ref_price=1.1, ts=_B1, units=0
    )
    assert b.positions.positions == []


def test_long_close_pnl_and_costs():
    b = _broker()
    b.enter_at_next_open(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.1000,
        ts=_B1,
        units=1000,
    )
    b.mark_position("EURUSD", 1.1100)
    # close on signal at mark
    trade = b.close_on_signal(symbol="EURUSD", price=1.1100, ts=_B1)
    assert trade is not None
    assert trade.gross_pnl == 10.0
    assert trade.net_pnl < trade.gross_pnl  # costs deducted
    assert trade.exit_reason == EXIT_SIGNAL
    assert b.positions.open_for("EURUSD") is None


def test_stop_loss_exit():
    b = _broker()
    b.enter_at_next_open(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.1000,
        ts=_B1,
        units=1000,
        stop_loss=1.0900,
        take_profit=1.1200,
    )
    trade = b.evaluate_exit(symbol="EURUSD", close=1.0850, ts=_B1)
    assert trade is not None
    assert trade.exit_reason == EXIT_SL
    assert trade.exit_price == 1.0900


def test_take_profit_exit():
    b = _broker()
    b.enter_at_next_open(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.1000,
        ts=_B1,
        units=1000,
        stop_loss=1.0900,
        take_profit=1.1200,
    )
    trade = b.evaluate_exit(symbol="EURUSD", close=1.1250, ts=_B1)
    assert trade is not None
    assert trade.exit_reason == EXIT_TP
    assert trade.exit_price == 1.1200


def test_evaluate_exit_noop_without_position():
    b = _broker()
    assert b.evaluate_exit(symbol="EURUSD", close=1.1, ts=_B1) is None


def test_state_and_drawdown():
    b = _broker()
    st = b.state(_B1)
    assert st.equity == b.start_equity
    assert st.cash == b.start_equity
    b.enter_at_next_open(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.1000,
        ts=_B1,
        units=10_000,
    )
    b.mark_position("EURUSD", 1.0500)  # draw down 5%
    b.state(_B1)
    assert b.max_drawdown_pct > 0.0


def test_cash_start_constant_sanity():
    assert CASH_START == 100_000.0


def test_trade_roundtrip_fields():
    b = _broker()
    b.enter_at_next_open(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.SHORT,
        ref_price=1.1000,
        ts=_B1,
        units=500,
    )
    trade = b.close_on_signal(symbol="EURUSD", price=1.0950, ts=_B1)
    assert isinstance(trade, Trade)
    assert trade.side == Direction.SHORT
    assert trade.entry_ts == _B1
