"""Unit tests: ledger read projections (Phase 14A).

The 14A live risk gate is built from *read-only* projections:
``load_open_position_rows`` (exposure numerator), ``load_realized_pnl_since``
(UTC-day realized PnL, inclusive-lo / exclusive-hi window), and
``list_pending_expired`` (PENDING orders whose sponsoring decision passed its
``valid_until``). These tests pin those semantics at the store/facade seam and
assert the facade (``LedgerBroker``) delegates to the store unchanged.

(Phase 14A)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.broker.ledger import LedgerBroker
from app.models.paper_ledger import (
    AccountSnapshotRow,
    PaperOrderRow,
    PaperOrderStatus,
    PaperPositionRow,
    PaperPositionStatus,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_T0 = datetime(2025, 6, 10, 8, 0, tzinfo=UTC)
_T1 = _T0 + timedelta(hours=2)
_T0_YESTERDAY = _T0 - timedelta(days=1)


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


def _open(symbol: str, *, units: str, entry: str) -> PaperPositionRow:
    return PaperPositionRow(
        id=uuid.uuid4(),
        order_id=uuid.uuid4(),
        symbol=symbol,
        timeframe="H1",
        side="LONG",
        units=Decimal(units),
        entry_price=Decimal(entry),
        entry_ts=_T0,
        costs=Decimal("0"),
        status=PaperPositionStatus.OPEN.value,
    )


def _closed(*, exit_ts: datetime, net_pnl: str) -> PaperPositionRow:
    return PaperPositionRow(
        id=uuid.uuid4(),
        order_id=uuid.uuid4(),
        symbol="EURUSD",
        timeframe="H1",
        side="LONG",
        units=Decimal("10000"),
        entry_price=Decimal("1.0000"),
        entry_ts=exit_ts - timedelta(hours=1),
        exit_price=Decimal("1.0000"),
        exit_ts=exit_ts,
        costs=Decimal("0"),
        net_pnl=Decimal(net_pnl),
        status=PaperPositionStatus.CLOSED.value,
    )


def _order(*, decision_id: uuid.UUID | None, status: str) -> PaperOrderRow:
    return PaperOrderRow(
        id=uuid.uuid4(),
        decision_id=decision_id,
        symbol="EURUSD",
        timeframe="H1",
        side="LONG",
        status=status,
    )


async def test_total_open_notional_sums_open_only() -> None:
    store = InMemoryStore()
    store.positions = [
        _open("EURUSD", units="10000", entry="1.0850"),
        _open("GBPUSD", units="5000", entry="1.2700"),
        _closed(exit_ts=_T1, net_pnl="-50"),
    ]
    broker = LedgerBroker(store=store)
    notional = await broker.total_open_notional()
    assert notional == pytest.approx(10_000.0 * 1.0850 + 5_000.0 * 1.2700)


async def test_realized_pnl_since_window_is_inclusive_lo_exclusive_hi() -> None:
    store = InMemoryStore()
    store.positions = [
        _closed(exit_ts=_T0_YESTERDAY, net_pnl="100"),  # before window (excluded)
        _closed(exit_ts=_T0, net_pnl="200"),  # lo bound (included)
        _closed(exit_ts=_T0 + timedelta(minutes=30), net_pnl="-300"),
        _closed(exit_ts=_T1, net_pnl="400"),  # hi bound (excluded)
        _open("EURUSD", units="1000", entry="1.1"),  # OPEN rows never counted
    ]
    broker = LedgerBroker(store=store)

    windowed = await broker.realized_pnl_since(start_ts=_T0, end_ts=_T1)
    assert windowed == pytest.approx(200.0 - 300.0)

    lifetime = await broker.realized_pnl_since(start_ts=_EPOCH, end_ts=_T1 + timedelta(days=1))
    assert lifetime == pytest.approx(100.0 + 200.0 - 300.0 + 400.0)


async def test_list_pending_expired_only_past_valid_untils_and_pending() -> None:
    stale = uuid.uuid4()
    cold = uuid.uuid4()
    store = InMemoryStore()
    store.decision_expiry = {stale: _T0, cold: _T1}
    store.orders = [
        _order(decision_id=stale, status=PaperOrderStatus.PENDING.value),  # expired + pending
        _order(decision_id=stale, status=PaperOrderStatus.FILLED.value),  # expired but filled
        _order(decision_id=stale, status=PaperOrderStatus.CANCELLED.value),  # already cancelled
        _order(decision_id=cold, status=PaperOrderStatus.PENDING.value),  # fresh decision
        _order(decision_id=None, status=PaperOrderStatus.PENDING.value),  # unsponsored
    ]
    broker = LedgerBroker(store=store)

    expired = await broker.list_pending_expired(now=_T1)
    assert [o.decision_id for o in expired] == [stale]


async def test_equity_props_follow_restored_snapshot() -> None:
    store = InMemoryStore()
    store.snapshots = [
        AccountSnapshotRow(
            id=uuid.uuid4(),
            ts=_T0,
            cash=Decimal("95000.000000"),
            equity=Decimal("95000.000000"),
            open_pnl=Decimal("0.000000"),
            realized_pnl=Decimal("-5000.000000"),
            peak_equity=Decimal("100000.000000"),
            drawdown_pct=Decimal("0.05000000"),
            margin_used=Decimal("0.000000"),
            extra={},
        )
    ]
    broker = LedgerBroker(store=store)
    await broker.restore_state()

    assert broker.start_equity == pytest.approx(100_000.0)
    assert broker.equity == pytest.approx(95_000.0)
    assert broker.peak_equity == pytest.approx(100_000.0)
    assert broker.max_drawdown_pct == pytest.approx(0.05)
