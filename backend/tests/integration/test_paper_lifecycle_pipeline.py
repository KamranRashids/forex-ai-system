"""Integration: Phase 13D paper lifecycle over real PostgreSQL.

Drives ``OrchestratorWorker.process_lifecycle`` — the single writer under the
token-guarded Redis lock — against the scratch DB migrated to head. A PAPER
decision row (real ``decisions`` row, so the deterministic fill-bar seam reads a
real ``bucket_ts``) sponsors a PENDING order, and the lifecycle fills / exits /
cancels at deterministic next-bar opens using stored candles. SAFE MODE is
preserved: paper-accounting tables only, no execution endpoint exists.

(Phase 13D)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

pytestmark = [pytest.mark.integration]

SYMBOL = "GBPUSD"
TF = "M15"
TF_MINUTES = 15
_SEED = 5

from app.data.timeframes import previous_closed_bucket  # noqa: E402

_BUCKET = previous_closed_bucket(datetime.now(UTC), "M15")


def _at(minutes: int) -> datetime:
    return _BUCKET + timedelta(minutes=minutes)


async def _seed_candles(db_sessionmaker: Any, closes: dict[int, float]) -> None:
    """Insert closed candles at ``_BUCKET + minutes`` with open == close."""
    from app.data.ingest import seed_instruments
    from app.models.candle import CandleRow

    async with db_sessionmaker() as session:
        inst = (await seed_instruments(session, [SYMBOL]))[SYMBOL]
        for minutes, close in sorted(closes.items()):
            session.add(
                CandleRow(
                    instrument_id=inst.id,
                    timeframe=TF,
                    ts=_at(minutes),
                    open=Decimal(str(close)),
                    high=Decimal(str(close)),
                    low=Decimal(str(close)),
                    close=Decimal(str(close)),
                    volume=100,
                    source="synthetic",
                    complete=True,
                    tf_minutes=TF_MINUTES,
                )
            )
        await session.commit()


def _orch_worker(db_sessionmaker: Any, fake_redis: Any) -> Any:
    from app.bus.publisher import RedisEventPublisher
    from app.workers.orchestrator_worker import OrchestratorWorker

    def broker_factory(session: Any) -> Any:
        from app.broker.ledger import LedgerBroker
        from app.broker.store import PostgresLedgerStore

        return LedgerBroker(store=PostgresLedgerStore(session=session), seed=_SEED)

    return OrchestratorWorker(
        session_factory=db_sessionmaker,
        redis=fake_redis,
        publisher=RedisEventPublisher(fake_redis, producer_name="orchestrator"),
        broker_factory=broker_factory,
    )


async def _sponsor_pending(
    db_sessionmaker: Any,
    *,
    direction: str = "LONG",
    minutes: int = 0,
    price: float = 1.10,
    units: float = 5_000.0,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> tuple[uuid.UUID, Any]:
    """Persist a PAPER decision + PENDING order (production submit seam)."""
    from app.agents.base import Direction
    from app.broker.ledger import LedgerBroker
    from app.broker.store import PostgresLedgerStore
    from app.models.decision import DecisionRow, DecisionStatus

    decision_id = uuid.uuid4()
    async with db_sessionmaker() as session:
        session.add(
            DecisionRow(
                id=decision_id,
                run_id="",
                symbol=SYMBOL,
                timeframe=TF,
                bucket_ts=_at(minutes),
                fused_direction=direction,
                confidence=Decimal("0.7000"),
                agreement=Decimal("0.7000"),
                status=DecisionStatus.PAPER.value,
                veto_code=None,
                veto_reason=None,
                inputs_hash="0" * 64,
                weights={},
                code_versions={},
                rationale=None,
                decision_at=_at(minutes),
                valid_until=_at(minutes + 120),
            )
        )
        await session.commit()

    async with db_sessionmaker() as session:
        broker = LedgerBroker(store=PostgresLedgerStore(session=session), seed=_SEED)
        order = await broker.submit_paper_order(
            symbol=SYMBOL,
            timeframe=TF,
            direction=Direction(direction),
            ref_price=price,
            ts=_at(minutes),
            units=units,
            decision_id=decision_id,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        await session.commit()
        assert order is not None
        assert order.status == "PENDING"
        return decision_id, order


async def _count_orders(db_sessionmaker: Any) -> int:
    from app.models.paper_ledger import PaperOrderRow

    async with db_sessionmaker() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(PaperOrderRow)
                .where(PaperOrderRow.symbol == SYMBOL)
            )
        )


async def _position_counts(db_sessionmaker: Any) -> dict[str, int]:
    from app.models.paper_ledger import PaperPositionRow, PaperPositionStatus

    async with db_sessionmaker() as session:
        statuses = (
            (
                await session.execute(
                    select(PaperPositionRow.status).where(PaperPositionRow.symbol == SYMBOL)
                )
            )
            .scalars()
            .all()
        )
    counts = {PaperPositionStatus.OPEN.value: 0, PaperPositionStatus.CLOSED.value: 0}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Phase 14A: PENDING-expiry sweep (cancel + release + restart-safe)
# ---------------------------------------------------------------------------


async def _metric_value(name: str) -> int:
    from prometheus_client import generate_latest

    value = 0
    for line in generate_latest().decode().splitlines():
        if line.startswith(name + " "):
            value = int(float(line.split()[-1]))
    return value


async def _sponsor_expired_pending(db_sessionmaker: Any) -> uuid.UUID:
    """Persist a PAPER decision whose ``valid_until`` already passed + PENDING order."""
    from app.agents.base import Direction
    from app.broker.ledger import LedgerBroker
    from app.broker.store import PostgresLedgerStore
    from app.models.decision import DecisionRow, DecisionStatus

    decision_id = uuid.uuid4()
    async with db_sessionmaker() as session:
        session.add(
            DecisionRow(
                id=decision_id,
                run_id="",
                symbol=SYMBOL,
                timeframe=TF,
                bucket_ts=_at(-60),
                fused_direction="LONG",
                confidence=Decimal("0.7000"),
                agreement=Decimal("0.7000"),
                status=DecisionStatus.PAPER.value,
                veto_code=None,
                veto_reason=None,
                inputs_hash="0" * 64,
                weights={},
                code_versions={},
                rationale=None,
                decision_at=_at(-60),
                valid_until=_at(-1),  # already passed by the time the sweep runs
            )
        )
        await session.commit()

    async with db_sessionmaker() as session:
        broker = LedgerBroker(store=PostgresLedgerStore(session=session), seed=_SEED)
        order = await broker.submit_paper_order(
            symbol=SYMBOL,
            timeframe=TF,
            direction=Direction.LONG,
            ref_price=1.10,
            ts=_at(-60),
            units=5_000.0,
            decision_id=decision_id,
        )
        await session.commit()
        assert order is not None
        assert order.status == "PENDING"
        return order.id


@pytest.mark.asyncio
async def test_lifecycle_expires_pending_past_valid_until(
    db_sessionmaker: Any, fake_redis: Any
) -> None:
    """A PENDING order past its decision's ``valid_until`` auto-cancels (14A).

    Cancel happens before the unit loop, releases the slot, never fills, and is
    restart-safe: a second pass cancels nothing and the counter does not move.
    """
    from app.models.paper_ledger import PaperOrderRow

    order_id = await _sponsor_expired_pending(db_sessionmaker)
    worker = _orch_worker(db_sessionmaker, fake_redis)
    before = await _metric_value("paper_pending_expired_total")

    result = await worker.process_lifecycle()

    assert result.errors == 0
    assert result.expired == 1
    assert result.fills == 0
    assert result.bars == 0
    assert result.units == 1  # the expired pending order still marks its unit slot
    assert await _metric_value("paper_pending_expired_total") == before + 1

    async with db_sessionmaker() as session:
        row = await session.get(PaperOrderRow, order_id)
        assert row is not None
        assert row.status == "CANCELLED"

    replay = await worker.process_lifecycle()
    assert replay.errors == 0
    assert replay.expired == 0
    assert replay.fills == 0
    assert replay.units == 0  # cancelled order released the slot; nothing re-discovered
    assert await _metric_value("paper_pending_expired_total") == before + 1


# ---------------------------------------------------------------------------
# Fill -> SL exit -> snapshot -> restart no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_fills_exits_and_snapshots(db_sessionmaker: Any, fake_redis: Any) -> None:
    """PENDING order fills at the next-bar open, then exits on an SL bar close."""
    await _seed_candles(db_sessionmaker, {0: 1.10, 15: 1.10, 30: 1.07})
    worker = _orch_worker(db_sessionmaker, fake_redis)

    await _sponsor_pending(db_sessionmaker, stop_loss=1.075, take_profit=1.12)
    result = await worker.process_lifecycle()

    assert result is not None
    assert result.reconcile_ok is True
    assert result.units == 1
    assert result.bars == 2  # fill bar + SL-hit bar
    assert result.fills == 1
    assert result.exits == 1  # stop_loss at the 1.07 close
    assert result.superseded == 0
    assert result.cancelled_missing == 0
    assert result.errors == 0

    assert await _count_orders(db_sessionmaker) == 1
    assert await _position_counts(db_sessionmaker) == {"OPEN": 0, "CLOSED": 1}

    from app.models.paper_ledger import PaperOrderRow, PaperPositionRow

    async with db_sessionmaker() as session:
        order = (
            (await session.execute(select(PaperOrderRow).where(PaperOrderRow.symbol == SYMBOL)))
            .scalars()
            .one()
        )
        closed = (
            (
                await session.execute(
                    select(PaperPositionRow).where(PaperPositionRow.symbol == SYMBOL)
                )
            )
            .scalars()
            .one()
        )
    assert order.status == "FILLED"
    assert closed.status == "CLOSED"
    assert closed.exit_reason == "stop_loss"

    from app.models.paper_ledger import AccountSnapshotRow

    async with db_sessionmaker() as session:
        snapshot_count = await session.scalar(select(func.count()).select_from(AccountSnapshotRow))
    # The lifecycle persisted observability snapshots (fill + exit bars).
    assert snapshot_count is not None and snapshot_count >= 1

    # A fresh cycle after a restart discovers nothing left to do.
    replay = await worker.process_lifecycle()
    assert replay is not None
    assert replay.units == 0
    assert replay.bars == 0


# ---------------------------------------------------------------------------
# Missing fill bar: cancel, never fabricate a price
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_cancels_missing_fill_bar(db_sessionmaker: Any, fake_redis: Any) -> None:
    """Fill bar == latest_closed but the candle is absent -> cancel missing_bar."""
    await _seed_candles(db_sessionmaker, {0: 1.10, 30: 1.10})  # 15-minute fill bar absent
    worker = _orch_worker(db_sessionmaker, fake_redis)

    await _sponsor_pending(db_sessionmaker, minutes=0)
    result = await worker.process_lifecycle()

    assert result is not None
    assert result.reconcile_ok is True
    assert result.units == 1
    assert result.cancelled_missing == 1
    assert result.fills == 0
    assert result.exits == 0
    assert result.errors == 0

    from app.models.paper_ledger import PaperOrderRow, PaperOrderStatus

    async with db_sessionmaker() as session:
        order = (
            (await session.execute(select(PaperOrderRow).where(PaperOrderRow.symbol == SYMBOL)))
            .scalars()
            .one()
        )
    assert order.status == PaperOrderStatus.CANCELLED.value
    assert await _position_counts(db_sessionmaker) == {"OPEN": 0, "CLOSED": 0}


# ---------------------------------------------------------------------------
# Opposing pending: close + flip at the next-bar open (one fill per bar)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_flips_opposing_pending(db_sessionmaker: Any, fake_redis: Any) -> None:
    """LONG (fill +15) then SHORT (fill +30): the +30 open closes + flips."""
    await _seed_candles(db_sessionmaker, {0: 1.10, 15: 1.10, 30: 1.10, 45: 1.10})
    worker = _orch_worker(db_sessionmaker, fake_redis)

    await _sponsor_pending(
        db_sessionmaker, direction="LONG", minutes=0, stop_loss=1.05, take_profit=1.20
    )
    await _sponsor_pending(
        db_sessionmaker, direction="SHORT", minutes=15, stop_loss=1.20, take_profit=1.05
    )
    result = await worker.process_lifecycle()

    assert result is not None
    assert result.reconcile_ok is True
    assert result.units == 1
    assert result.fills == 2  # LONG fill + SHORT flip-fill
    assert result.superseded == 0
    assert result.errors == 0
    assert await _count_orders(db_sessionmaker) == 2
    assert await _position_counts(db_sessionmaker) == {"OPEN": 1, "CLOSED": 1}

    from app.models.paper_ledger import PaperPositionRow, PaperPositionStatus

    async with db_sessionmaker() as session:
        positions = (
            (
                await session.execute(
                    select(PaperPositionRow).where(PaperPositionRow.symbol == SYMBOL)
                )
            )
            .scalars()
            .all()
        )
    by_status = {p.status: p for p in positions}
    assert by_status[PaperPositionStatus.OPEN.value].side == "SHORT"
    assert by_status[PaperPositionStatus.CLOSED.value].side == "LONG"
    assert by_status[PaperPositionStatus.CLOSED.value].exit_reason == "signal"
