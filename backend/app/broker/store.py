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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_ledger import (
    AccountSnapshotRow,
    PaperOrderRow,
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
