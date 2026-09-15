"""Unit tests: Phase 13D paper lifecycle domain (DB-free).

These run against the pure ``PaperLifecycle`` over an in-memory ``LedgerStore``
so the strict unit coverage gate sees the arbitration / recovery / catch-up /
snapshot logic directly (no PostgreSQL required). Every simulated value comes
from the same deterministic ``PaperBroker`` math the backtester uses:

- a PENDING order (decision-linked) fills at its deterministic next-bar open;
- at most one fill per (symbol, bar): first actionable candidate acts, later
  candidates are superseded; opposing signals flip (close at the open, then
  fill), same-side keep-policy supersedes;
- SL/TP is evaluated at the *fill bar's close* too;
- catch-up is ascending, throttled by ``max_bars``, never a skip;
- account snapshots are ts-deduped under the single writer, optional interval.

(Phase 13D)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.agents.base import Direction
from app.broker.ledger import LedgerBroker
from app.broker.lifecycle import (
    CANCEL_MISSING_BAR,
    Bar,
    PaperLifecycle,
    PendingCandidate,
    arbitrate_fills,
    candidates_for_bar,
    catchup_window,
    fill_stamp,
    missing_bar_candidates,
)
from app.broker.paper import PaperBroker
from app.models.paper_ledger import (
    AccountSnapshotRow,
    PaperOrderRow,
    PaperOrderStatus,
    PaperPositionRow,
    PaperPositionStatus,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_B0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_B1 = _B0 + timedelta(hours=1)
_B2 = _B0 + timedelta(hours=2)
_B3 = _B0 + timedelta(hours=3)
_B4 = _B0 + timedelta(hours=4)


class InMemoryStore:
    """Pure-Python LedgerStore used by the DB-free unit tests."""

    def __init__(self) -> None:
        self.orders: list[PaperOrderRow] = []
        self.positions: list[PaperPositionRow] = []
        self.snapshots: list[AccountSnapshotRow] = []
        self.decision_buckets: dict[uuid.UUID, datetime] = {}

    async def save_order(self, order: PaperOrderRow) -> None:
        self.orders.append(order)

    async def update_order(self, order: PaperOrderRow) -> None:
        for i, existing in enumerate(self.orders):
            if existing.id == order.id:
                self.orders[i] = order
                return
        self.orders.append(order)

    async def list_pending_orders(self) -> list[PaperOrderRow]:
        pending = [o for o in self.orders if o.status == PaperOrderStatus.PENDING.value]
        return sorted(pending, key=lambda o: (_get_created(o), o.id))

    async def get_decision_bucket(self, decision_id: uuid.UUID | None) -> datetime | None:
        return None if decision_id is None else self.decision_buckets.get(decision_id)

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


def _get_created(order: PaperOrderRow) -> datetime:
    return order.created_at if order.created_at is not None else _EPOCH


def _register(store: InMemoryStore, decision_id: uuid.UUID, bucket: datetime) -> None:
    store.decision_buckets[decision_id] = bucket


async def _submit(
    broker: LedgerBroker,
    store: InMemoryStore,
    *,
    direction: Direction = Direction.LONG,
    bucket: datetime = _B0,
    symbol: str = "EURUSD",
    price: float = 1.10,
    units: float = 10_000.0,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> PaperOrderRow | None:
    decision_id = uuid.uuid4()
    _register(store, decision_id, bucket)
    return await broker.submit_paper_order(
        symbol=symbol,
        timeframe="H1",
        direction=direction,
        ref_price=price,
        ts=bucket,
        units=units,
        decision_id=decision_id,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )


# ---------------------------------------------------------------------------
# Pure helpers (deterministic fill stamps / windows / arbitration).
# ---------------------------------------------------------------------------


def test_fill_stamp_is_decision_bucket_plus_tf_seconds():
    order = PaperOrderRow(id=uuid.uuid4(), symbol="EURUSD", timeframe="H1", side="LONG")
    assert fill_stamp(order, _B0) == _B1
    assert fill_stamp(order, _B1) == _B2


def test_catchup_window_throttles_never_skips():
    stamps = [_B0 + timedelta(hours=i) for i in range(100)]
    to_process, depth = catchup_window(stamps, 24)
    assert to_process == stamps[:24]
    assert depth == 76

    to_process, depth = catchup_window(stamps, 25)
    assert len(to_process) == 25
    assert depth == 75

    to_process, depth = catchup_window(stamps, 1)
    assert to_process == [stamps[0]]
    assert depth == 99

    # A zero/negative cap degrades to "at least one" — never a zero-window skip.
    assert catchup_window(stamps, 0)[0] == [stamps[0]]
    assert catchup_window(stamps, -3)[0] == [stamps[0]]


def test_catchup_window_dedupes_and_sorts():
    stamps = [_B2, _B0, _B1, _B0]
    to_process, depth = catchup_window(stamps, 10)
    assert to_process == [_B0, _B1, _B2]
    assert depth == 0


def test_candidates_for_bar_filters_and_sorts():
    o1 = PaperOrderRow(id=uuid.uuid4(), symbol="EURUSD", timeframe="H1", side="LONG")
    o2 = PaperOrderRow(id=uuid.uuid4(), symbol="EURUSD", timeframe="H1", side="SHORT")
    o3 = PaperOrderRow(id=uuid.uuid4(), symbol="EURUSD", timeframe="H1", side="LONG")
    cands = [
        PendingCandidate(order=o1, fill_stamp=_B1),
        PendingCandidate(order=o2, fill_stamp=_B2),
        PendingCandidate(order=o3, fill_stamp=_B1),
    ]
    at_b1 = candidates_for_bar(cands, _B1)
    assert [c.order.id for c in at_b1] == sorted(
        [c.order.id for c in (cands[0], cands[2])]
    )
    assert candidates_for_bar(cands, _B3) == []


def test_arbitrate_fills_no_open_first_candidate_acts():
    cands = [
        PendingCandidate(
            order=PaperOrderRow(
                id=uuid.uuid4(), symbol="EURUSD", timeframe="H1", side="LONG"
            ),
            fill_stamp=_B1,
        ),
        PendingCandidate(
            order=PaperOrderRow(
                id=uuid.uuid4(), symbol="EURUSD", timeframe="H1", side="LONG"
            ),
            fill_stamp=_B1,
        ),
    ]
    actions = arbitrate_fills(None, cands)
    assert [(c.order.side, a) for c, a in actions] == [("LONG", "fill"), ("LONG", "superseded")]


def test_arbitrate_fills_same_side_supersedes_then_opposing_flips():
    cands = [
        PendingCandidate(
            order=PaperOrderRow(
                id=uuid.uuid4(), symbol="EURUSD", timeframe="H1", side="LONG"
            ),
            fill_stamp=_B1,
        ),
        PendingCandidate(
            order=PaperOrderRow(
                id=uuid.uuid4(), symbol="EURUSD", timeframe="H1", side="SHORT"
            ),
            fill_stamp=_B1,
        ),
    ]
    actions = arbitrate_fills(Direction.LONG, cands)
    assert [a for _, a in actions] == ["superseded", "flip"]


def test_arbitrate_fills_opposing_flips_without_open():
    cand = PendingCandidate(
        order=PaperOrderRow(id=uuid.uuid4(), symbol="EURUSD", timeframe="H1", side="SHORT"),
        fill_stamp=_B1,
    )
    actions = arbitrate_fills(Direction.LONG, [cand])
    assert actions == [(cand, "flip")]


def test_arbitrate_fills_at_most_one_fill():
    cands = [
        PendingCandidate(
            order=PaperOrderRow(id=uuid.uuid4(), symbol="EURUSD", timeframe="H1", side=s),
            fill_stamp=_B1,
        )
        for s in ("LONG", "SHORT", "LONG")
    ]
    actions = arbitrate_fills(None, cands)
    assert [a for _, a in actions] == ["fill", "superseded", "superseded"]


def test_missing_bar_candidates_never_fabricate_price():
    late = PendingCandidate(
        order=PaperOrderRow(id=uuid.uuid4(), symbol="EURUSD", timeframe="H1", side="LONG"),
        fill_stamp=_B3,
    )
    missing = PendingCandidate(
        order=PaperOrderRow(id=uuid.uuid4(), symbol="EURUSD", timeframe="H1", side="LONG"),
        fill_stamp=_B1,
    )
    present = PendingCandidate(
        order=PaperOrderRow(id=uuid.uuid4(), symbol="EURUSD", timeframe="H1", side="LONG"),
        fill_stamp=_B2,
    )
    built = missing_bar_candidates(
        [late, missing, present], [_B2], latest_closed=_B2
    )
    # Only ``missing``: ``late`` is not due yet, ``present`` has a closed candle.
    assert [c.order.id for c in built] == [missing.order.id]


# ---------------------------------------------------------------------------
# End-to-end: PENDING -> next-bar open fill, parity, snapshot.
# ---------------------------------------------------------------------------


async def test_pending_order_fills_at_next_bar_open():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    order = await _submit(broker, store, stop_loss=1.08, take_profit=1.14)
    assert order is not None
    assert order.status == PaperOrderStatus.PENDING.value

    lifecycle = PaperLifecycle(store=store, broker=broker)
    candidates = await lifecycle.pending_candidates()
    result = await lifecycle.process_unit_bar(
        symbol="EURUSD",
        timeframe="H1",
        bar=Bar(ts=_B1, open=1.10, close=1.11),
        candidates=candidates,
    )

    assert result.filled == 1
    assert order.status == PaperOrderStatus.FILLED.value
    assert float(order.filled_price) == pytest.approx(1.10, abs=1e-9)
    assert order.filled_at == _B1

    pos = await store.get_open_position("EURUSD")
    assert pos is not None
    assert pos.status == PaperPositionStatus.OPEN.value
    assert pos.order_id == order.id
    assert float(pos.entry_price) == pytest.approx(1.10, abs=1e-9)
    assert pos.entry_ts == _B1
    # A state-changing bar writes an account snapshot (ts = bar bucket).
    assert [s.ts for s in store.snapshots] == [_B1]


async def test_fill_matches_standalone_paperbroker():
    seed = 42
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=seed)
    order = await _submit(broker, store, price=1.10, stop_loss=1.08)
    lifecycle = PaperLifecycle(store=store, broker=broker)
    candidates = await lifecycle.pending_candidates()
    await lifecycle.process_unit_bar(
        symbol="EURUSD",
        timeframe="H1",
        bar=Bar(ts=_B1, open=1.10, close=1.11),
        candidates=candidates,
    )

    paper = PaperBroker(seed=seed)
    paper.enter_at_next_open(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.10,
        ts=_B1,
        stop_loss=1.08,
        units=10_000.0,
    )
    pos = await store.get_open_position("EURUSD")
    paper_pos = paper.positions.open_for("EURUSD")
    assert pos is not None and paper_pos is not None
    assert float(pos.entry_price) == pytest.approx(paper_pos.entry_price, abs=1e-9)
    assert float(pos.costs) == pytest.approx(paper_pos.costs, abs=1e-9)
    # Same seed, same inputs -> same deterministic entry costs.
    assert float(order.costs) == pytest.approx(paper_pos.costs, abs=1e-9)


async def test_fill_bar_sl_tp_evaluated_at_same_bar_close():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    order = await _submit(broker, store, stop_loss=1.08, take_profit=1.14)
    lifecycle = PaperLifecycle(store=store, broker=broker)
    candidates = await lifecycle.pending_candidates()

    result = await lifecycle.process_unit_bar(
        symbol="EURUSD",
        timeframe="H1",
        bar=Bar(ts=_B1, open=1.10, close=1.07),
        candidates=candidates,
    )

    assert result.filled == 1
    assert result.exits == 1
    assert order.status == PaperOrderStatus.FILLED.value
    closed = await store.list_closed_positions()
    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"
    assert float(closed[0].exit_price) == pytest.approx(1.08, abs=1e-9)
    assert await store.get_open_position("EURUSD") is None


async def test_fill_bar_not_closed_yet_leaves_order_pending():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    order = await _submit(broker, store, bucket=_B1)  # fill stamp = _B2
    lifecycle = PaperLifecycle(store=store, broker=broker)
    candidates = await lifecycle.pending_candidates()

    result = await lifecycle.process_unit_bar(
        symbol="EURUSD",
        timeframe="H1",
        bar=Bar(ts=_B1, open=1.10, close=1.11),
        candidates=candidates,
    )

    assert result.filled == 0
    assert result.snapshot_written is False
    assert order.status == PaperOrderStatus.PENDING.value
    assert await store.get_open_position("EURUSD") is None
    assert not store.snapshots


# ---------------------------------------------------------------------------
# Arbitration end-to-end: flip, supersede, missing bar.
# ---------------------------------------------------------------------------


async def test_opposing_pending_flips_existing_position():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    first = await _submit(broker, store, bucket=_B0)  # LONG fills at _B1
    assert first is not None
    lifecycle = PaperLifecycle(store=store, broker=broker)
    candidates = await lifecycle.pending_candidates()
    await lifecycle.process_unit_bar(
        symbol="EURUSD",
        timeframe="H1",
        bar=Bar(ts=_B1, open=1.10, close=1.11),
        candidates=candidates,
    )
    assert await store.get_open_position("EURUSD") is not None

    # Opposing signal submits while the position is open (keep-policy allows it).
    second = await _submit(broker, store, direction=Direction.SHORT, bucket=_B1, price=1.10)
    assert second is not None
    candidates = await lifecycle.pending_candidates()
    assert len(candidates) == 1
    assert candidates[0].fill_stamp == _B2

    result = await lifecycle.process_unit_bar(
        symbol="EURUSD",
        timeframe="H1",
        bar=Bar(ts=_B2, open=1.11, close=1.09),
        candidates=candidates,
    )

    assert result.filled == 1
    closed = await store.list_closed_positions()
    assert len(closed) == 1
    assert closed[0].exit_reason == "signal"
    assert float(closed[0].exit_price) == pytest.approx(1.11, abs=1e-9)
    assert second.status == PaperOrderStatus.FILLED.value
    pos = await store.get_open_position("EURUSD")
    assert pos is not None and pos.side == "SHORT"
    assert float(pos.entry_price) == pytest.approx(1.11, abs=1e-9)


async def test_multiple_pending_one_fill_rest_superseded():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    first = await _submit(broker, store, bucket=_B0)
    second = await _submit(broker, store, bucket=_B0)
    assert first is not None and second is not None
    lifecycle = PaperLifecycle(store=store, broker=broker)
    candidates = await lifecycle.pending_candidates()

    result = await lifecycle.process_unit_bar(
        symbol="EURUSD",
        timeframe="H1",
        bar=Bar(ts=_B1, open=1.10, close=1.11),
        candidates=candidates,
    )

    assert result.filled == 1
    assert result.superseded == 1
    # Oldest-first arbitration: exactly one order fills, the other is cancelled.
    assert {first.status, second.status} == {
        PaperOrderStatus.FILLED.value,
        PaperOrderStatus.CANCELLED.value,
    }
    assert (first.filled_price is None) != (second.filled_price is None)
    # Never two positions for one (symbol, bar).
    assert len(await store.list_open_positions()) == 1


async def test_superseded_and_filled_orders_leave_broker_pending_queue():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    await _submit(broker, store, bucket=_B0)
    await _submit(broker, store, bucket=_B0)
    lifecycle = PaperLifecycle(store=store, broker=broker)
    candidates = await lifecycle.pending_candidates()
    await lifecycle.process_unit_bar(
        symbol="EURUSD",
        timeframe="H1",
        bar=Bar(ts=_B1, open=1.10, close=1.11),
        candidates=candidates,
    )
    assert broker.pending == []


async def test_missing_bar_marked_for_cancellation():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    order = await _submit(broker, store, bucket=_B0)  # fill stamp _B1
    assert order is not None
    lifecycle = PaperLifecycle(store=store, broker=broker)
    candidates = await lifecycle.pending_candidates()
    # The fill bar never produced a closed candle, but an older candle exists.
    doomed = missing_bar_candidates(candidates, [], latest_closed=_B1)
    assert [c.order.id for c in doomed] == [order.id]

    for cand in doomed:
        assert (
            await broker.cancel_pending(cand.order, reason=CANCEL_MISSING_BAR) is not None
        )
    assert order.status == PaperOrderStatus.CANCELLED.value
    assert broker.pending == []
    assert await lifecycle.pending_candidates() == []


# ---------------------------------------------------------------------------
# Recovery identity / frontier / restore.
# ---------------------------------------------------------------------------


async def test_frontier_captures_fill_bars_and_resume_after():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    await _submit(broker, store, bucket=_B0)

    lifecycle = PaperLifecycle(store=store, broker=broker)
    frontier = await lifecycle.frontier(symbol="EURUSD", timeframe="H1")
    assert frontier.active
    assert frontier.open_position is None
    assert frontier.resume_after is None
    assert frontier.fill_bars == frozenset({_B1})

    await lifecycle.process_unit_bar(
        symbol="EURUSD", timeframe="H1", bar=Bar(ts=_B1, open=1.10, close=1.11),
        candidates=await lifecycle.pending_candidates(),
    )
    frontier = await lifecycle.frontier(
        symbol="EURUSD",
        timeframe="H1",
        open_positions=await store.list_open_positions(),
    )
    assert frontier.pending == ()
    assert frontier.open_position is not None
    assert frontier.resume_after == _B1


async def test_restore_rebuilds_pending_queue_and_fills_later():
    seed = 7
    store = InMemoryStore()
    first = LedgerBroker(store=store, seed=seed)
    order = await _submit(first, store, bucket=_B0)
    assert order is not None

    # Fresh process restores the pending order from the ledger before driving bars.
    broker = LedgerBroker(store=store, seed=seed)
    lifecycle = PaperLifecycle(store=store, broker=broker)
    assert list(broker.pending) == []
    await broker.restore_state()
    assert [o.id for o in broker.pending] == [order.id]
    assert [c.order.id for c in (await lifecycle.pending_candidates())] == [order.id]

    result = await lifecycle.process_unit_bar(
        symbol="EURUSD", timeframe="H1", bar=Bar(ts=_B1, open=1.10, close=1.11),
        candidates=await lifecycle.pending_candidates(),
    )
    assert result.filled == 1
    assert order.status == PaperOrderStatus.FILLED.value


# ---------------------------------------------------------------------------
# Reconcilation (single-writer view == ledger view).
# ---------------------------------------------------------------------------


async def test_reconcile_passes_when_in_sync():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    await _submit(broker, store, bucket=_B0)
    await broker.restore_state()
    lifecycle = PaperLifecycle(store=store, broker=broker)
    assert await lifecycle.reconcile() is True

    await lifecycle.process_unit_bar(
        symbol="EURUSD", timeframe="H1", bar=Bar(ts=_B1, open=1.10, close=1.11),
        candidates=await lifecycle.pending_candidates(),
    )
    assert await lifecycle.reconcile() is True


async def test_reconcile_fails_on_ledger_drift():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    await _submit(broker, store, bucket=_B0)
    # A broker that never restored its state is out of sync with the store.
    drift_broker = LedgerBroker(store=store, seed=7)
    lifecycle = PaperLifecycle(store=store, broker=drift_broker)
    assert await lifecycle.reconcile() is False
    await drift_broker.restore_state()
    assert await lifecycle.reconcile() is True


async def test_reconcile_fails_on_position_drift():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    lifecycle = PaperLifecycle(store=store, broker=broker)
    store.positions.append(
        PaperPositionRow(
            id=uuid.uuid4(),
            order_id=uuid.uuid4(),
            symbol="EURUSD",
            timeframe="H1",
            side="LONG",
            units=Decimal("10000.000000"),
            entry_price=Decimal("1.10000000"),
            entry_ts=_B1,
            costs=Decimal("0"),
            status=PaperPositionStatus.OPEN.value,
        )
    )
    assert await lifecycle.reconcile() is False


# ---------------------------------------------------------------------------
# Snapshots: ts-dedup, interval throttle, idempotent reprocessing.
# ---------------------------------------------------------------------------


async def test_snapshot_ts_dedup_on_reprocess_of_same_bar():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    await _submit(broker, store, bucket=_B0)
    lifecycle = PaperLifecycle(store=store, broker=broker)
    candidates = await lifecycle.pending_candidates()
    bar = Bar(ts=_B1, open=1.10, close=1.11)

    first = await lifecycle.process_unit_bar(
        symbol="EURUSD", timeframe="H1", bar=bar, candidates=candidates
    )
    assert first.snapshot_written is True
    second = await lifecycle.process_unit_bar(
        symbol="EURUSD", timeframe="H1", bar=bar, candidates=candidates
    )
    assert second.filled == 0
    assert second.snapshot_written is False

    assert len(store.snapshots) == 1
    assert len(await store.list_open_positions()) == 1
    filled = sum(1 for o in store.orders if o.status == PaperOrderStatus.FILLED.value)
    assert filled == 1


async def test_snapshot_interval_throttles_writes():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    await _submit(broker, store, bucket=_B0)  # fill stamp _B1
    lifecycle = PaperLifecycle(
        store=store, broker=broker, snapshot_interval_seconds=7200
    )
    candidates = await lifecycle.pending_candidates()
    for ts, close in [(_B1, 1.11), (_B2, 1.12), (_B3, 1.13)]:
        await lifecycle.process_unit_bar(
            symbol="EURUSD", timeframe="H1", bar=Bar(ts=ts, open=1.10, close=close),
            candidates=candidates,
        )
    # Written at _B1 (first) and _B3 (>= _B1 + 2h); _B2 falls inside the interval.
    assert [s.ts for s in store.snapshots] == [_B1, _B3]


# ---------------------------------------------------------------------------
# Aggregate processing / catch-up.
# ---------------------------------------------------------------------------


async def test_process_unit_catch_up_throttled_ascending():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    await _submit(broker, store, bucket=_B0)  # fill at _B1
    lifecycle = PaperLifecycle(store=store, broker=broker)
    bars = [Bar(ts=_B0 + timedelta(hours=i), open=1.10, close=1.11) for i in range(1, 26)]

    summary = await lifecycle.process_unit(
        symbol="EURUSD", timeframe="H1", bars=bars, max_bars=24
    )

    assert summary.bars_processed == 24
    assert summary.depth_remaining == 1
    assert summary.fills == 1
    assert summary.superseded == 0
    assert summary.exits == 0
    order = store.orders[0]
    assert order.status == PaperOrderStatus.FILLED.value


async def test_process_unit_all_bars_when_no_throttle():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    await _submit(broker, store, bucket=_B0)
    lifecycle = PaperLifecycle(store=store, broker=broker)
    bars = [Bar(ts=_B0 + timedelta(hours=i), open=1.10, close=1.11) for i in range(1, 4)]

    summary = await lifecycle.process_unit(symbol="EURUSD", timeframe="H1", bars=bars)

    assert summary.bars_processed == 3
    assert summary.depth_remaining == 0
    assert summary.fills == 1


async def test_process_unit_empty_bars_is_noop():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    summary = await PaperLifecycle(store=store, broker=broker).process_unit(
        symbol="EURUSD", timeframe="H1", bars=[]
    )
    assert summary.bars_processed == 0
    assert summary.depth_remaining == 0
    assert not store.orders


async def test_unresolvable_destination_is_left_pending_not_processed():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    order = await broker.submit_paper_order(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.10,
        ts=_B0,
        units=10_000.0,
        decision_id=None,
    )
    assert order is not None
    lifecycle = PaperLifecycle(store=store, broker=broker)
    candidates = await lifecycle.pending_candidates()
    assert candidates == []
    result = await lifecycle.process_unit_bar(
        symbol="EURUSD",
        timeframe="H1",
        bar=Bar(ts=_B1, open=1.10, close=1.11),
        candidates=candidates,
    )
    assert result.filled == 0
    assert order.status == PaperOrderStatus.PENDING.value
