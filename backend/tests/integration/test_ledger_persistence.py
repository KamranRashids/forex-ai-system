"""Integration: paper-ledger persistence over real PostgreSQL (Phase 13, Phase A).

Runs on the scratch DB migrated to head (so migration 0008 is applied) and
exercises ``PostgresLedgerStore`` plus the full ``LedgerBroker`` lifecycle:
open -> SL exit -> equity snapshot -> restore across sessions. SAFE MODE is
preserved: these tables are paper-accounting only and no execution endpoint
exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.agents.base import Direction
from app.broker.ledger import LedgerBroker
from app.broker.store import PostgresLedgerStore
from app.models.paper_ledger import (
    AccountSnapshotRow,
    PaperOrderRow,
    PaperPositionRow,
    PaperPositionStatus,
)
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration

_T0 = datetime(2024, 3, 1, tzinfo=UTC)
_T1 = _T0 + timedelta(hours=1)
_T2 = _T0 + timedelta(hours=2)


async def test_store_round_trip_persists_and_lists(db_sessionmaker) -> None:
    async with db_sessionmaker() as session:
        store = PostgresLedgerStore(session=session)
        order = PaperOrderRow(
            symbol="EURUSD",
            timeframe="H1",
            side="LONG",
            order_type="NEXT_OPEN",
            units=Decimal("10000"),
            requested_price=Decimal("1.0850"),
            filled_price=Decimal("1.0850"),
            costs=Decimal("0.155000"),
            seed=0,
            filled_at=_T0,
        )
        await store.save_order(order)
        assert order.id is not None

        position = PaperPositionRow(
            order_id=order.id,
            symbol="EURUSD",
            timeframe="H1",
            side="LONG",
            units=Decimal("10000"),
            entry_price=Decimal("1.0850"),
            entry_ts=_T0,
            costs=Decimal("0.155000"),
            status=PaperPositionStatus.OPEN.value,
        )
        await store.save_position(position)
        await session.commit()

        found = await store.get_open_position("EURUSD")
        assert found is not None
        assert found.id == position.id
        opens = await store.list_open_positions()
        assert len(opens) == 1
        assert opens[0].symbol == "EURUSD"

        snapshot = AccountSnapshotRow(
            ts=_T1,
            cash=Decimal("99999.845000"),
            equity=Decimal("100000.000000"),
            open_pnl=Decimal("0.155000"),
            realized_pnl=Decimal("0.000000"),
            peak_equity=Decimal("100000.000000"),
            drawdown_pct=Decimal("0.00000000"),
            margin_used=Decimal("0.000000"),
            extra={},
        )
        await store.save_snapshot(snapshot)
        await session.commit()

    async with db_sessionmaker() as session:
        latest = await PostgresLedgerStore(session=session).latest_snapshot()
        assert latest is not None
        assert float(latest.cash) == pytest.approx(99999.845, abs=1e-6)


async def test_db_enforces_one_open_position_per_symbol(db_sessionmaker) -> None:
    async with db_sessionmaker() as session:
        store = PostgresLedgerStore(session=session)
        first = PaperOrderRow(
            symbol="EURUSD",
            timeframe="H1",
            side="LONG",
            order_type="NEXT_OPEN",
            units=Decimal("1000"),
        )
        await store.save_order(first)
        await store.save_position(
            PaperPositionRow(
                order_id=first.id,
                symbol="EURUSD",
                timeframe="H1",
                side="LONG",
                units=Decimal("1000"),
                entry_price=Decimal("1.1000"),
                entry_ts=_T0,
                status=PaperPositionStatus.OPEN.value,
            )
        )

        second = PaperOrderRow(
            symbol="EURUSD",
            timeframe="H1",
            side="SHORT",
            order_type="NEXT_OPEN",
            units=Decimal("1000"),
        )
        await store.save_order(second)
        with pytest.raises(IntegrityError):
            await store.save_position(
                PaperPositionRow(
                    order_id=second.id,
                    symbol="EURUSD",
                    timeframe="H1",
                    side="SHORT",
                    units=Decimal("1000"),
                    entry_price=Decimal("1.1000"),
                    entry_ts=_T0,
                    status=PaperPositionStatus.OPEN.value,
                )
            )
        await session.rollback()


async def test_ledger_broker_round_trip_across_sessions(db_sessionmaker) -> None:
    snap: AccountSnapshotRow | None = None
    async with db_sessionmaker() as session:
        broker = LedgerBroker(store=PostgresLedgerStore(session=session), seed=9)
        order = await broker.submit_paper_order(
            symbol="GBPUSD",
            timeframe="H1",
            direction=Direction.LONG,
            ref_price=1.2700,
            ts=_T0,
            units=5_000.0,
            stop_loss=1.2600,
        )
        assert order is not None
        assert order.status == "PENDING"
        position = await broker.fill_pending(order, open_price=1.2700, ts=_T0)
        assert position is not None
        trade = await broker.evaluate_exit(symbol="GBPUSD", close=1.2550, ts=_T1)
        assert trade is not None
        assert trade.exit_reason == "stop_loss"
        snap = await broker.equity_snapshot(ts=_T2)
        assert float(snap.realized_pnl) == pytest.approx(trade.net_pnl, abs=1e-6)
        await session.commit()

    assert snap is not None
    async with db_sessionmaker() as session:
        restored = LedgerBroker(store=PostgresLedgerStore(session=session), seed=9)
        await restored.restore_state()
        state = restored.state(_T2)
        assert len(state.open_positions.positions) == 0
        assert float(state.cash) == pytest.approx(float(snap.cash), abs=1e-6)
        again = await restored.equity_snapshot(ts=_T2)
        assert float(again.realized_pnl) == pytest.approx(float(snap.realized_pnl), abs=1e-6)
        await session.commit()
