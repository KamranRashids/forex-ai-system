"""Unit tests: ledger-derived live risk gate (Phase 14A).

Builds ``build_ledger_gate`` over an in-memory ledger + ``LedgerBroker`` and
asserts the gate mirrors ``driver._gate``:

- exposure     = Σ(OPEN units x entry_price) / equity;
- correlation  = LONG-only notional of the candidate's currency basket / equity;
- daily-loss   = max(0, -Σ CLOSED net_pnl in the UTC day) / equity;
- drawdown     = the ledger's tracked max drawdown from peak;

and the view-model: PENDING (unfilled) orders consume no capital
(``INCLUDE_PENDING_IN_EXPOSURE``), keeping backtest parity. The returned
``LedgerGateView`` carries the raw projections the engine's reporting mirror
and the ``gate_snapshot`` observability line need.

(Phase 14A)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.agents.base import Direction
from app.broker.ledger import LedgerBroker
from app.decisions.ledger_gate import INCLUDE_PENDING_IN_EXPOSURE, build_ledger_gate
from app.models.paper_ledger import (
    AccountSnapshotRow,
    PaperOrderRow,
    PaperOrderStatus,
    PaperPositionRow,
    PaperPositionStatus,
)

_T0 = datetime(2025, 6, 10, 8, 0, tzinfo=UTC)
_T1 = _T0 + timedelta(hours=2)
_T2 = _T0 - timedelta(hours=36)  # the day before


class InMemoryStore:
    """Pure-Python LedgerStore used by the DB-free unit tests."""

    def __init__(self) -> None:
        self.orders: list[PaperOrderRow] = []
        self.positions: list[PaperPositionRow] = []
        self.snapshots: list[AccountSnapshotRow] = []
        self.decision_expiry: dict[uuid.UUID, datetime] = {}

    async def save_order(self, order: PaperOrderRow) -> None:
        self.orders.append(order)

    async def update_order(self, order: PaperOrderRow) -> None:
        for i, existing in enumerate(self.orders):
            if existing.id == order.id:
                self.orders[i] = order
                return
        self.orders.append(order)

    async def list_pending_orders(self) -> list[PaperOrderRow]:
        return [order for order in self.orders if order.status == PaperOrderStatus.PENDING.value]

    async def get_decision_bucket(self, decision_id: uuid.UUID | None) -> datetime | None:
        return None if decision_id is None else _T0

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

    async def load_open_position_rows(self) -> list[PaperPositionRow]:
        return await self.list_open_positions()

    async def load_realized_pnl_since(self, start_ts: datetime, end_ts: datetime) -> Decimal:
        return sum(
            (p.net_pnl or Decimal("0"))
            for p in self.positions
            if p.status == PaperPositionStatus.CLOSED.value
            and p.exit_ts is not None
            and start_ts <= p.exit_ts < end_ts
        )

    async def list_pending_expired(self, now: datetime) -> list[PaperOrderRow]:
        return [
            o
            for o in self.orders
            if o.status == PaperOrderStatus.PENDING.value
            and o.decision_id is not None
            and (self.decision_expiry.get(o.decision_id) or datetime.min.replace(tzinfo=UTC)) < now
        ]

    async def list_closed_positions(self) -> list[PaperPositionRow]:
        return [p for p in self.positions if p.status == PaperPositionStatus.CLOSED.value]

    async def update_position(self, position: PaperPositionRow) -> None:
        return None

    async def save_snapshot(self, snapshot: AccountSnapshotRow) -> None:
        self.snapshots.append(snapshot)

    async def latest_snapshot(self) -> AccountSnapshotRow | None:
        return self.snapshots[-1] if self.snapshots else None


def _open(
    symbol: str,
    *,
    side: str = "LONG",
    units: float = 10_000.0,
    entry: float = 1.0850,
) -> PaperPositionRow:
    return PaperPositionRow(
        id=uuid.uuid4(),
        order_id=uuid.uuid4(),
        symbol=symbol,
        timeframe="H1",
        side=side,
        units=Decimal(str(units)),
        entry_price=Decimal(str(entry)),
        entry_ts=_T0,
        costs=Decimal("0"),
        status=PaperPositionStatus.OPEN.value,
    )


def _closed(
    *,
    exit_ts: datetime,
    net_pnl: str,
    symbol: str = "EURUSD",
    side: str = "LONG",
) -> PaperPositionRow:
    return PaperPositionRow(
        id=uuid.uuid4(),
        order_id=uuid.uuid4(),
        symbol=symbol,
        timeframe="H1",
        side=side,
        units=Decimal("10000"),
        entry_price=Decimal("1.0000"),
        entry_ts=exit_ts - timedelta(hours=1),
        exit_price=Decimal("1.0000"),
        exit_ts=exit_ts,
        costs=Decimal("0"),
        net_pnl=Decimal(net_pnl),
        status=PaperPositionStatus.CLOSED.value,
    )


async def test_fresh_account_gate_is_all_zero() -> None:
    broker = LedgerBroker(store=InMemoryStore())
    view = await build_ledger_gate(broker=broker, symbol="EURUSD", now=_T1)

    gate = view.gate
    assert gate.exposure_used_pct == 0.0
    assert gate.correlation_used_pct == 0.0
    assert gate.daily_loss_used_pct == 0.0
    assert gate.drawdown_used_pct == 0.0
    assert gate.correlation_triggered is False
    assert view.equity == pytest.approx(100_000.0)
    assert view.peak_equity == pytest.approx(100_000.0)
    assert view.realized_today == pytest.approx(0.0)
    assert view.cumulative_realized == pytest.approx(0.0)
    assert view.open_notional == pytest.approx(0.0)
    assert view.open_position_count == 0


async def test_open_positions_drive_exposure_and_long_only_basket() -> None:
    store = InMemoryStore()
    # EURUSD LONG 10k @ 1.0850 => 10,850; GBPUSD LONG 5k @ 1.2700 => 6,350.
    store.positions = [
        _open("EURUSD", side="LONG", units=10_000.0, entry=1.0850),
        _open("GBPUSD", side="LONG", units=5_000.0, entry=1.2700),
        _open("EURUSD", side="SHORT", units=10_000.0, entry=1.0850),
    ]
    broker = LedgerBroker(store=store)
    view = await build_ledger_gate(broker=broker, symbol="EURUSD", now=_T1)

    notional = 10_000.0 * 1.0850 + 5_000.0 * 1.2700 + 10_000.0 * 1.0850
    assert view.open_notional == pytest.approx(notional)
    assert view.open_position_count == 3
    assert view.gate.exposure_used_pct == pytest.approx(notional / 100_000.0)
    # Basket = LONG notional only, sharing a currency with EUR/USD: both the
    # EURUSD and GBPUSD LONG positions (shared USD); the SHORT never counts.
    assert view.gate.correlation_used_pct == pytest.approx((10_850.0 + 6_350.0) / 100_000.0)
    assert view.gate.correlation_triggered is True


async def test_pending_unfilled_orders_consume_no_exposure() -> None:
    assert INCLUDE_PENDING_IN_EXPOSURE is False
    store = InMemoryStore()
    broker = LedgerBroker(store=store)
    order = await broker.submit_paper_order(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.10,
        ts=_T0,
        units=10_000.0,
        decision_id=uuid.uuid4(),
    )
    assert order is not None

    view = await build_ledger_gate(broker=broker, symbol="EURUSD", now=_T0)
    assert view.gate.exposure_used_pct == 0.0
    assert view.open_notional == pytest.approx(0.0)
    assert view.open_position_count == 0


async def test_daily_loss_uses_utc_day_window_and_floors_at_zero() -> None:
    store = InMemoryStore()
    store.positions = [
        _closed(exit_ts=_T1, net_pnl="-2000"),  # today: loss
        _closed(exit_ts=_T1 - timedelta(minutes=30), net_pnl="500"),  # today: gain
        _closed(exit_ts=_T2, net_pnl="9999"),  # previous day: excluded from daily
    ]
    broker = LedgerBroker(store=store)
    view = await build_ledger_gate(broker=broker, symbol="EURUSD", now=_T1)

    assert view.realized_today == pytest.approx(-1500.0)  # today only
    assert view.cumulative_realized == pytest.approx(8499.0)  # windowed sum is total-only
    assert view.gate.daily_loss_used_pct == pytest.approx(1500.0 / 100_000.0)


async def test_daily_loss_floors_positive_day_at_zero() -> None:
    store = InMemoryStore()
    store.positions = [_closed(exit_ts=_T1, net_pnl="700")]
    broker = LedgerBroker(store=store)
    view = await build_ledger_gate(broker=broker, symbol="EURUSD", now=_T1)

    assert view.realized_today == pytest.approx(700.0)
    assert view.gate.daily_loss_used_pct == 0.0  # profitable day never "loses"


async def test_drawdown_engages_from_ledger_peak() -> None:
    store = InMemoryStore()
    broker = LedgerBroker(store=store)
    order = await broker.submit_paper_order(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.0850,
        ts=_T0,
        units=10_000.0,
        decision_id=uuid.uuid4(),
        stop_loss=1.0805,
    )
    assert order is not None
    position = await broker.fill_pending(order, open_price=1.0850, ts=_T0)
    assert position is not None
    trade = await broker.evaluate_exit(symbol="EURUSD", close=1.0800, ts=_T1)
    assert trade is not None and trade.net_pnl < 0
    await broker.equity_snapshot(ts=_T1)

    restored = LedgerBroker(store=store)
    await restored.restore_state()
    assert restored.equity < 100_000.0
    assert restored.max_drawdown_pct > 0.0

    view = await build_ledger_gate(broker=restored, symbol="EURUSD", now=_T1)
    assert view.gate.drawdown_used_pct == pytest.approx(restored.max_drawdown_pct)
    assert view.gate.daily_loss_used_pct == pytest.approx(
        max(0.0, -trade.net_pnl) / restored.equity
    )
    assert view.equity == pytest.approx(restored.equity)
    assert view.peak_equity == pytest.approx(restored.peak_equity)


async def test_no_snapshot_falls_back_to_start_equity() -> None:
    swapped = LedgerBroker(store=InMemoryStore(), start_equity=50_000.0)
    view = await build_ledger_gate(broker=swapped, symbol="EURUSD", now=_T0)
    assert view.equity == pytest.approx(50_000.0)
    assert view.gate.daily_loss_used_pct == 0.0
