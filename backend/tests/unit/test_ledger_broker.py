"""Unit tests: LedgerBroker pure orchestration over an in-memory ledger store.

These run DB-free (unit gate) using an in-memory ``LedgerStore`` so the broker's
orchestration — pending submit/fill, SL/TP exit persistence, equity snapshots,
and state restore — is covered without PostgreSQL. Every simulated value is
produced by the same ``PaperBroker`` math the backtester uses; the parity tests
here assert the ledger results equal a standalone ``PaperBroker``.

Entry is deferred (Phase 13D): a decision submits a PENDING order and
``fill_pending`` fills it at the next bar's open. The ``_open`` helper performs
submit + fill so the existing exit/equity/restore assertions keep their
original timeline.

(Phase 13, Phase A, Phase 13D)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.agents.base import Direction
from app.broker.ledger import LedgerBroker
from app.broker.paper import PaperBroker
from app.models.paper_ledger import (
    AccountSnapshotRow,
    PaperOrderRow,
    PaperOrderStatus,
    PaperPositionRow,
    PaperPositionStatus,
)

_T0 = datetime(2024, 3, 1, tzinfo=UTC)
_T1 = _T0 + timedelta(hours=1)
_T2 = _T0 + timedelta(hours=2)
_T3 = _T0 + timedelta(hours=3)


class InMemoryStore:
    """Pure-Python LedgerStore used by the DB-free unit tests."""

    def __init__(self) -> None:
        self.orders: list[PaperOrderRow] = []
        self.positions: list[PaperPositionRow] = []
        self.snapshots: list[AccountSnapshotRow] = []

    async def save_order(self, order: PaperOrderRow) -> None:
        self.orders.append(order)

    async def update_order(self, order: PaperOrderRow) -> None:
        for i, existing in enumerate(self.orders):
            if existing.id == order.id:
                self.orders[i] = order
                return

    async def list_pending_orders(self) -> list[PaperOrderRow]:
        return [order for order in self.orders if order.status == PaperOrderStatus.PENDING.value]

    async def get_decision_bucket(self, decision_id: uuid.UUID | None) -> datetime | None:
        return _T0 if decision_id is not None else None

    async def save_position(self, position: PaperPositionRow) -> None:
        self.positions.append(position)

    async def get_open_position(self, symbol: str) -> PaperPositionRow | None:
        return next(
            (
                p
                for p in self.positions
                if p.symbol == symbol.upper() and p.status == PaperPositionStatus.OPEN.value
            ),
            None,
        )

    async def list_open_positions(self) -> list[PaperPositionRow]:
        return [p for p in self.positions if p.status == PaperPositionStatus.OPEN.value]

    async def list_closed_positions(self) -> list[PaperPositionRow]:
        return [p for p in self.positions if p.status == PaperPositionStatus.CLOSED.value]

    async def update_position(self, position: PaperPositionRow) -> None:
        return None

    async def save_snapshot(self, snapshot: AccountSnapshotRow) -> None:
        self.snapshots.append(snapshot)

    async def latest_snapshot(self) -> AccountSnapshotRow | None:
        return self.snapshots[-1] if self.snapshots else None


async def _open(
    broker: LedgerBroker,
    *,
    symbol: str = "EURUSD",
    direction: Direction = Direction.LONG,
    price: float = 1.0850,
    units: float = 10_000.0,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> PaperOrderRow | None:
    """Submit a PENDING order and immediately fill it (deferred-entry path)."""
    order = await broker.submit_paper_order(
        symbol=symbol,
        timeframe="H1",
        direction=direction,
        ref_price=price,
        ts=_T0,
        units=units,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
    if order is None:
        return None
    pos = await broker.fill_pending(order, open_price=price, ts=_T0)
    assert pos is not None, "fill_pending failed unexpectedly"
    return order


async def test_open_persists_order_and_position():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)

    order = await _open(broker, stop_loss=1.0750, take_profit=1.0950)

    assert order is not None
    assert order.status == PaperOrderStatus.FILLED.value
    assert float(order.filled_price) == pytest.approx(1.0850, abs=1e-9)
    assert float(order.requested_price) == pytest.approx(1.0850, abs=1e-9)
    assert float(order.costs) > 0.0  # deterministic entry cost deducted

    pos = await store.get_open_position("EURUSD")
    assert pos is not None
    assert pos.status == PaperPositionStatus.OPEN.value
    assert pos.order_id == order.id
    assert float(pos.units) == pytest.approx(10_000.0, abs=1e-9)
    assert float(pos.entry_price) == pytest.approx(1.0850, abs=1e-9)
    assert float(pos.stop_loss) == pytest.approx(1.0750, abs=1e-9)

    state = broker.state(_T1)
    assert state.cash == pytest.approx(100_000.0 - float(order.costs), abs=1e-6)


async def test_fill_matches_standalone_paperbroker():
    seed = 42
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=seed)
    paper = PaperBroker(seed=seed)

    await _open(broker, symbol="GBPUSD", price=1.2700, units=5_000.0, stop_loss=1.2600)
    paper.enter_at_next_open(
        symbol="GBPUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.2700,
        ts=_T0,
        stop_loss=1.2600,
        units=5_000.0,
    )

    pos = await store.get_open_position("GBPUSD")
    paper_pos = paper.positions.open_for("GBPUSD")
    assert pos is not None and paper_pos is not None
    assert float(pos.entry_price) == pytest.approx(paper_pos.entry_price, abs=1e-9)
    assert float(pos.costs) == pytest.approx(paper_pos.costs, abs=1e-9)

    ledger_trade = await broker.evaluate_exit(symbol="GBPUSD", close=1.2500, ts=_T1)
    paper_trade = paper.evaluate_exit(symbol="GBPUSD", close=1.2500, ts=_T1)
    assert ledger_trade is not None and paper_trade is not None
    assert ledger_trade.exit_reason == paper_trade.exit_reason == "stop_loss"
    assert ledger_trade.exit_price == pytest.approx(paper_trade.exit_price, abs=1e-9)
    assert ledger_trade.net_pnl == pytest.approx(paper_trade.net_pnl, abs=1e-9)


async def test_open_rejected_when_position_already_open():
    store = InMemoryStore()
    broker = LedgerBroker(store=store)

    first = await _open(broker)
    second = await _open(broker)

    assert first is not None
    assert second is None
    assert len(store.orders) == 1
    assert len([p for p in store.positions if p.status == PaperPositionStatus.OPEN.value]) == 1


async def test_open_rejected_for_flat_or_nonpositive_units():
    store = InMemoryStore()
    broker = LedgerBroker(store=store)

    for direction, units in [
        (Direction.FLAT, 100.0),
        (Direction.LONG, 0.0),
        (Direction.LONG, -5.0),
    ]:
        assert await _open(broker, direction=direction, units=units) is None

    assert not store.orders
    assert not store.positions


async def test_evaluate_exit_stop_loss_persists_close():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    await _open(broker, stop_loss=1.0750, take_profit=1.0950)

    trade = await broker.evaluate_exit(symbol="EURUSD", close=1.0700, ts=_T1)

    assert trade is not None
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(1.0750, abs=1e-9)

    assert await store.get_open_position("EURUSD") is None
    closed = [p for p in store.positions if p.status == PaperPositionStatus.CLOSED.value]
    assert len(closed) == 1
    assert float(closed[0].exit_price) == pytest.approx(1.0750, abs=1e-9)
    assert closed[0].exit_reason == "stop_loss"
    assert float(closed[0].net_pnl) == pytest.approx(trade.net_pnl, abs=1e-9)
    # Closed rows carry full round-trip costs (entry + exit).
    assert float(closed[0].costs) == pytest.approx(trade.costs, abs=1e-9)


async def test_close_on_signal_persists_close():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=3)
    await _open(broker, symbol="AUDUSD", price=0.6600, units=4_000.0)

    trade = await broker.close_on_signal(symbol="AUDUSD", price=0.6580, ts=_T1)

    assert trade is not None
    assert trade.exit_reason == "signal"
    closed = [p for p in store.positions if p.status == PaperPositionStatus.CLOSED.value]
    assert len(closed) == 1
    assert float(closed[0].exit_price) == pytest.approx(0.6580, abs=1e-9)
    assert float(closed[0].net_pnl) == pytest.approx(trade.net_pnl, abs=1e-9)


async def test_evaluate_exit_returns_none_when_no_position():
    store = InMemoryStore()
    broker = LedgerBroker(store=store)
    assert await broker.evaluate_exit(symbol="EURUSD", close=1.0, ts=_T1) is None


async def test_persist_close_noop_when_open_row_missing():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=5)
    await _open(broker, stop_loss=1.0750)
    store.positions.clear()  # simulate a missing/desynced ledger row

    trade = await broker.evaluate_exit(symbol="EURUSD", close=1.0700, ts=_T1)
    assert trade is not None  # broker-level close still happens
    assert not store.positions  # nothing persisted, no crash


async def test_equity_snapshot_reflects_marks_and_realized():
    start = 50_000.0
    store = InMemoryStore()
    broker = LedgerBroker(store=store, start_equity=start)
    await _open(broker, symbol="USDJPY", price=150.0, units=1_000.0)

    await broker.mark_position(symbol="USDJPY", price=151.0)
    up = await broker.equity_snapshot(ts=_T1)

    assert float(up.open_pnl) == pytest.approx(1_000.0, abs=1e-6)
    assert float(up.realized_pnl) == 0.0
    assert float(up.cash) < start

    await broker.close_on_signal(symbol="USDJPY", price=149.0, ts=_T2)
    down = await broker.equity_snapshot(ts=_T2)

    assert float(down.open_pnl) == 0.0
    assert float(down.realized_pnl) < 0.0
    # Gross basis is the 1.0 move x 1,000 units; realized nets round-trip costs.
    assert float(down.realized_pnl) == pytest.approx(-1_000.0, abs=20.0)


async def test_restore_state_rebuilds_open_position_and_realized():
    seed = 11
    store = InMemoryStore()
    first = LedgerBroker(store=store, seed=seed)
    await _open(first, symbol="EURUSD", stop_loss=1.0500)
    await _open(first, symbol="GBPUSD", direction=Direction.SHORT, price=1.2700, units=5_000.0)
    trade = await first.evaluate_exit(symbol="EURUSD", close=1.0400, ts=_T1)
    assert trade is not None
    before = await first.equity_snapshot(ts=_T2)

    restored = LedgerBroker(store=store, seed=seed)
    await restored.restore_state()
    state = restored.state(_T3)

    assert len(state.open_positions.positions) == 1
    assert state.open_positions.open_for("GBPUSD") is not None
    assert float(state.cash) == pytest.approx(float(before.cash), abs=1e-6)
    assert float(state.equity) == pytest.approx(float(before.equity), abs=1e-6)

    after = await restored.equity_snapshot(ts=_T3)
    assert float(after.realized_pnl) == pytest.approx(float(before.realized_pnl), abs=1e-6)


async def test_restore_state_rejects_second_open_for_restored_symbol():
    store = InMemoryStore()
    first = LedgerBroker(store=store, seed=2)
    await _open(first, symbol="EURUSD")
    await first.equity_snapshot(ts=_T1)

    restored = LedgerBroker(store=store, seed=2)
    await restored.restore_state()
    assert await _open(restored, symbol="EURUSD") is None
    assert len([p for p in store.positions if p.status == PaperPositionStatus.OPEN.value]) == 1


async def test_restore_state_on_empty_ledger_keeps_defaults():
    broker = LedgerBroker(store=InMemoryStore(), start_equity=10_000.0)
    await broker.restore_state()
    state = broker.state(_T0)
    assert float(state.cash) == pytest.approx(10_000.0, abs=1e-9)
    assert float(state.equity) == pytest.approx(10_000.0, abs=1e-9)
    assert state.open_positions.positions == []
    assert state.trades == []


async def test_submit_persists_pending_order_without_position():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)

    order = await broker.submit_paper_order(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.0850,
        ts=_T0,
        units=10_000.0,
        stop_loss=1.0750,
        take_profit=1.0950,
    )

    assert order is not None
    assert order.status == PaperOrderStatus.PENDING.value
    assert order.filled_price is None
    assert float(order.costs) == 0.0
    assert await store.get_open_position("EURUSD") is None
    assert not store.positions
    assert len(store.orders) == 1
    assert broker.pending == [order]


async def test_fill_pending_rejects_non_pending_order():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)

    order = await broker.submit_paper_order(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.0850,
        ts=_T0,
        units=10_000.0,
    )
    assert order is not None
    order.status = PaperOrderStatus.CANCELLED.value  # superseded downstream

    assert await broker.fill_pending(order, open_price=1.0850, ts=_T0) is None
    assert await store.get_open_position("EURUSD") is None


async def test_cancel_pending_persists_cancelled():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)

    order = await broker.submit_paper_order(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.0850,
        ts=_T0,
        units=10_000.0,
    )
    assert order is not None
    assert await broker.cancel_pending(order, reason="missing_bar") is not None

    assert order.status == PaperOrderStatus.CANCELLED.value
    assert broker.pending == []
    assert store.orders[0].status == PaperOrderStatus.CANCELLED.value
    assert await store.get_open_position("EURUSD") is None


async def test_restore_state_rebuilds_pending_queue_oldest_first():
    store = InMemoryStore()
    first = LedgerBroker(store=store, seed=3)
    recent = await first.submit_paper_order(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.0850,
        ts=_T0,
        units=10_000.0,
        decision_id=uuid.uuid4(),
    )
    older = await first.submit_paper_order(
        symbol="GBPUSD",
        timeframe="H1",
        direction=Direction.SHORT,
        ref_price=1.2700,
        ts=_T0,
        units=5_000.0,
        decision_id=uuid.uuid4(),
    )
    assert recent is not None and older is not None
    recent.created_at = _T1
    older.created_at = _T0

    restored = LedgerBroker(store=store, seed=3)
    await restored.restore_state()

    assert [o.symbol for o in restored.pending] == ["GBPUSD", "EURUSD"]
    # A redelivered decision for one of the pending orders is rejected.
    dup = await restored.submit_paper_order(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.0850,
        ts=_T0,
        units=10_000.0,
        decision_id=recent.decision_id,
    )
    assert dup is None
