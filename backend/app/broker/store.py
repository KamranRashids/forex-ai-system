"""PostgreSQL-backed paper ledger store (Phase 13, Phase A; DB-I/O shell).

Deliberately thin: it persists/fetches paper orders, positions, and account
snapshots. There is no simulation math here (that lives in ``PaperBroker`` via
``LedgerBroker``) and no execution logic. It is exercised behaviorally by the
integration suite against real PostgreSQL; the pure orchestration in
``app/broker/ledger.py`` is covered by the strict unit coverage gate with an
in-memory store.

SAFE MODE: persistence only. No order routing, no broker connection, and no
live-execution path anywhere in this module.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_ledger import (
    AccountSnapshotRow,
    PaperOrderRow,
    PaperOrderStatus,
    PaperPositionRow,
    PaperPositionStatus,
)


class PostgresLedgerStore:
    """Async store over the paper-ledger tables via an AsyncSession."""

    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session

    async def save_order(self, order: PaperOrderRow) -> None:
        self._session.add(order)
        await self._session.flush()

    async def update_order(self, order: PaperOrderRow) -> None:
        await self._session.flush()

    async def list_pending_orders(self) -> list[PaperOrderRow]:
        """Pending (not yet filled/cancelled) orders, oldest first."""
        result = await self._session.execute(
            select(PaperOrderRow)
            .where(PaperOrderRow.status == PaperOrderStatus.PENDING.value)
            .order_by(PaperOrderRow.created_at, PaperOrderRow.id)
        )
        return list(result.scalars().all())

    async def get_decision_bucket(self, decision_id: uuid.UUID | None) -> datetime | None:
        """The bucket start of the decision that sponsors ``decision_id``.

        PENDING order rows do not carry their decision's bucket; the lifecycle
        needs ``decision.bucket_ts + tf_seconds`` to derive the deterministic
        fill bar, so the store resolves it from the sponsoring decision.
        """
        if decision_id is None:
            return None
        from app.models.decision import DecisionRow

        ts = await self._session.scalar(
            select(DecisionRow.bucket_ts).where(DecisionRow.id == decision_id)
        )
        return ts

    async def save_position(self, position: PaperPositionRow) -> None:
        self._session.add(position)
        await self._session.flush()

    async def get_open_position(self, symbol: str) -> PaperPositionRow | None:
        result = await self._session.execute(
            select(PaperPositionRow)
            .where(
                PaperPositionRow.symbol == symbol.upper(),
                PaperPositionRow.status == PaperPositionStatus.OPEN.value,
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_open_positions(self) -> list[PaperPositionRow]:
        result = await self._session.execute(
            select(PaperPositionRow).where(
                PaperPositionRow.status == PaperPositionStatus.OPEN.value
            )
        )
        return list(result.scalars().all())

    async def load_open_position_rows(self) -> list[PaperPositionRow]:
        """Open position rows — the live risk gate's exposure source (14A).

        Read-only projection identical to :meth:`list_open_positions`; kept as a
        distinct name so the gate/tests never depend on the write-oriented
        accessor that the lifecycle also uses.
        """
        return await self.list_open_positions()

    async def load_realized_pnl_since(self, start_ts: datetime, end_ts: datetime) -> Decimal:
        """Sum of ``net_pnl`` over CLOSED positions exited in ``[start, end)``.

        The restart-safe, derivation-only source for the daily realized-loss
        gate (no running accumulator to drift): same UTC-day semantics the
        backtest driver keys on.
        """
        total = await self._session.scalar(
            select(func.coalesce(func.sum(PaperPositionRow.net_pnl), 0)).where(
                PaperPositionRow.status == PaperPositionStatus.CLOSED.value,
                PaperPositionRow.exit_ts.is_not(None),
                PaperPositionRow.exit_ts >= start_ts,
                PaperPositionRow.exit_ts < end_ts,
            )
        )
        return Decimal(str(total))

    async def list_pending_expired(self, now: datetime) -> list[PaperOrderRow]:
        """PENDING orders whose sponsoring decision passed its ``valid_until``.

        A decision past its window (``bucket_ts + 4 x tf``) that never filled is
        permanently un-fillable; the sweep releases its one-open-per-symbol
        capacity. Idempotent — only PENDING rows are returned.
        """
        from app.models.decision import DecisionRow

        expired_ids = select(DecisionRow.id).where(DecisionRow.valid_until < now)
        result = await self._session.execute(
            select(PaperOrderRow)
            .where(
                PaperOrderRow.status == PaperOrderStatus.PENDING.value,
                PaperOrderRow.decision_id.is_not(None),
                PaperOrderRow.decision_id.in_(expired_ids),
            )
            .order_by(PaperOrderRow.created_at, PaperOrderRow.id)
        )
        return list(result.scalars().all())

    async def list_closed_positions(self) -> list[PaperPositionRow]:
        result = await self._session.execute(
            select(PaperPositionRow)
            .where(PaperPositionRow.status == PaperPositionStatus.CLOSED.value)
            .order_by(PaperPositionRow.exit_ts)
        )
        return list(result.scalars().all())

    async def update_position(self, position: PaperPositionRow) -> None:
        await self._session.flush()

    async def save_snapshot(self, snapshot: AccountSnapshotRow) -> None:
        self._session.add(snapshot)
        await self._session.flush()

    async def latest_snapshot(self) -> AccountSnapshotRow | None:
        result = await self._session.execute(
            select(AccountSnapshotRow).order_by(AccountSnapshotRow.ts.desc()).limit(1)
        )
        return result.scalar_one_or_none()
