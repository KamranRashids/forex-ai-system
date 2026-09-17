"""Unit tests: PENDING-expiry staleness sweep (Phase 14A).

An unfilled PENDING order whose sponsoring decision passed ``valid_until`` is
stale: the slot the backtest would have filled never opens, yet without a sweep
the order sits PENDING forever. The 14A lifecycle sweep cancels exactly those
orders — idempotent, restart-safe, one transaction per cycle (all-or-nothing
batch; a failure rolls the whole batch back so nothing is silently skipped,
retried next cycle). Same policy family as ``CANCEL_MISSING_BAR``, time-based
rather than bar-based.

Covers the store/broker seam and the worker's ``_cancel_expired_pending``.

(Phase 14A)
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from app.broker.ledger import LedgerBroker
from app.decisions.ledger_gate import CANCEL_EXPIRED_PENDING
from app.models.paper_ledger import (
    AccountSnapshotRow,
    PaperOrderRow,
    PaperOrderStatus,
    PaperPositionRow,
    PaperPositionStatus,
)
from app.workers.orchestrator_worker import OrchestratorWorker

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_T0 = datetime(2025, 6, 10, 8, 0, tzinfo=UTC)
_T1 = _T0 + timedelta(hours=2)
_NOW = datetime.now(UTC)
_T_OLD = _NOW - timedelta(days=1)
_T_FUTURE = _NOW + timedelta(days=365)


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


class TransactionalStore(InMemoryStore):
    """Staging store: updates land only via ``flush_updates()`` — persistence
    sits behind an explicit commit seam, so a mid-batch failure leaves the
    ledger untouched (mirrors the DB transaction the worker relies on)."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0
        self.fail_at = -1
        self._staged: list[PaperOrderRow] = []

    async def list_pending_expired(self, now: datetime) -> list[PaperOrderRow]:
        rows = await super().list_pending_expired(now)
        return [copy.deepcopy(r) for r in rows]

    async def update_order(self, order: PaperOrderRow) -> None:
        self.attempts += 1
        if self.attempts == self.fail_at:
            raise RuntimeError("boom")
        self._staged.append(copy.deepcopy(order))

    async def flush_updates(self) -> None:
        for staged in self._staged:
            await super().update_order(staged)
        self._staged.clear()


def _sponsored(store: InMemoryStore, *, decision_id: uuid.UUID, expiry: datetime) -> PaperOrderRow:
    store.decision_expiry[decision_id] = expiry
    order = PaperOrderRow(
        id=uuid.uuid4(),
        decision_id=decision_id,
        symbol="EURUSD",
        timeframe="H1",
        side="LONG",
        status=PaperOrderStatus.PENDING.value,
    )
    store.orders.append(order)
    return order


class FakeSession:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            await self.commit()
        else:
            await self.rollback()
        return False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class FakeSessionFactory:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.last: FakeSession | None = None

    def __call__(self) -> FakeSession:
        self.last = FakeSession(self.store)
        return self.last


def _worker(store: InMemoryStore) -> OrchestratorWorker:
    factory = FakeSessionFactory(store)
    broker = LedgerBroker(store=store)
    worker = OrchestratorWorker(
        session_factory=factory,  # type: ignore[arg-type]
        redis=MagicMock(),  # type: ignore[arg-type]
        publisher=MagicMock(),  # type: ignore[arg-type]
        broker_factory=lambda _session: broker,
    )
    worker._fake_session_factory = factory  # type: ignore[attr-defined]
    return worker


async def test_expired_pending_cancelled_and_cold_kept() -> None:
    store = InMemoryStore()
    cold = _sponsored(store, decision_id=uuid.uuid4(), expiry=_T1)
    stale = _sponsored(store, decision_id=uuid.uuid4(), expiry=_T0)
    broker = LedgerBroker(store=store)

    expired = await broker.list_pending_expired(now=_T1)
    assert [o.id for o in expired] == [stale.id]

    assert await broker.cancel_pending(stale, reason=CANCEL_EXPIRED_PENDING) is stale
    assert stale.status == PaperOrderStatus.CANCELLED.value

    still_open: list[PaperOrderRow] = await broker.store.list_pending_orders()
    assert [o.id for o in still_open] == [cold.id]
    assert await broker.list_pending_expired(now=_T1) == []


async def test_cancel_expired_is_idempotent() -> None:
    store = InMemoryStore()
    stale = _sponsored(store, decision_id=uuid.uuid4(), expiry=_T0)
    broker = LedgerBroker(store=store)

    first = await broker.cancel_pending(stale, reason=CANCEL_EXPIRED_PENDING)
    second = await broker.cancel_pending(stale, reason=CANCEL_EXPIRED_PENDING)
    assert first is stale
    assert second is None  # already CANCELLED -> no-op
    assert stale.status == PaperOrderStatus.CANCELLED.value


async def test_unsponsored_pending_never_expires() -> None:
    store = InMemoryStore()
    store.orders = [
        PaperOrderRow(
            id=uuid.uuid4(),
            decision_id=None,
            symbol="EURUSD",
            timeframe="H1",
            side="LONG",
            status=PaperOrderStatus.PENDING.value,
        )
    ]
    broker = LedgerBroker(store=store)
    assert await broker.list_pending_expired(now=_T_FUTURE) == []


# ---------------------------------------------------------------------------
# Worker-level batch sweep (all-or-nothing rollback)
# ---------------------------------------------------------------------------


def _metric_value(name: str) -> float:
    from prometheus_client import generate_latest

    text = generate_latest().decode()
    for line in text.splitlines():
        if line.startswith(name) and " " in line and not line.startswith(name + "_"):
            return float(line.split()[-1])
    return 0.0


async def test_worker_sweep_cancels_expired_commits_and_counts() -> None:
    store = InMemoryStore()
    _sponsored(store, decision_id=uuid.uuid4(), expiry=_T_OLD)
    _sponsored(store, decision_id=uuid.uuid4(), expiry=_T_OLD)
    _sponsored(store, decision_id=uuid.uuid4(), expiry=_T_FUTURE)
    worker = _worker(store)

    before = _metric_value("paper_pending_expired_total")
    cancelled = await worker._cancel_expired_pending()

    assert cancelled == 2
    assert worker._fake_session_factory.last is not None
    assert worker._fake_session_factory.last.committed is True
    assert _metric_value("paper_pending_expired_total") == before + 2
    assert len(await store.list_pending_orders()) == 1


async def test_worker_sweep_rolls_back_batch_on_failure() -> None:
    store = TransactionalStore()
    _sponsored(store, decision_id=uuid.uuid4(), expiry=_T_OLD)
    _sponsored(store, decision_id=uuid.uuid4(), expiry=_T_OLD)
    store.fail_at = 2  # second cancel update raises -> whole batch rolls back
    worker = _worker(store)

    with pytest.raises(RuntimeError, match="boom"):
        await worker._cancel_expired_pending()

    assert worker._fake_session_factory.last is not None
    assert worker._fake_session_factory.last.rolled_back is True
    # Nothing landed in the ledger: both orders stay PENDING for next cycle.
    pending = await store.list_pending_orders()
    assert len(pending) == 2
    assert all(o.status == PaperOrderStatus.PENDING.value for o in pending)
