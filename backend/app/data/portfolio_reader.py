"""Read-only portfolio queries over the Phase A paper ledger (Phase 13, Phase B).

Every function performs SELECTs only against ``orders_paper``, ``positions``
and ``account_snapshots``. Nothing in this module can create an order, mutate a
position or snapshot, or reach a broker — it is the observable read surface for
the SAFE MODE paper ledger.

SAFE MODE: strictly observational. No executor wiring, no order creation, no
trading/risk logic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class PortfolioPosition:
    """One position row joined to its originating order's decision."""

    id: uuid.UUID
    order_id: uuid.UUID
    decision_id: uuid.UUID | None
    symbol: str
    timeframe: str
    side: str
    units: float
    entry_price: float
    entry_ts: datetime
    stop_loss: float | None
    take_profit: float | None
    status: str
    exit_price: float | None
    exit_ts: datetime | None
    exit_reason: str | None
    gross_pnl: float | None
    net_pnl: float | None


@dataclass(frozen=True, slots=True)
class PortfolioOrder:
    """One paper order."""

    id: uuid.UUID
    decision_id: uuid.UUID | None
    symbol: str
    timeframe: str
    side: str
    order_type: str
    status: str
    units: float
    requested_price: float | None
    filled_price: float | None
    costs: float
    stop_loss: float | None
    take_profit: float | None
    filled_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PortfolioEquityPoint:
    """One point-in-time account snapshot."""

    ts: datetime
    cash: float
    equity: float
    open_pnl: float
    realized_pnl: float
    peak_equity: float
    drawdown_pct: float
    margin_used: float


@dataclass(frozen=True, slots=True)
class PortfolioSummary:
    """Latest snapshot plus open counts; zeros when the ledger is empty."""

    as_of: datetime | None
    cash: float
    equity: float
    open_pnl: float
    realized_pnl: float
    peak_equity: float
    drawdown_pct: float
    margin_used: float
    open_positions: int
    open_orders: int


_POSITION_STATUSES: frozenset[str] = frozenset(s.value for s in PaperPositionStatus)
_ORDER_STATUSES: frozenset[str] = frozenset(s.value for s in PaperOrderStatus)


def _f(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _checked_status(status: str | None, allowed: frozenset[str], label: str) -> str | None:
    """Validate a status filter; ``None`` means "no filter"."""
    if status is None:
        return None
    if status not in allowed:
        raise ValueError(f"invalid {label} status: {status!r}")
    return status


def _position(row: PaperPositionRow, decision_id: uuid.UUID | None) -> PortfolioPosition:
    return PortfolioPosition(
        id=row.id,
        order_id=row.order_id,
        decision_id=decision_id,
        symbol=row.symbol,
        timeframe=row.timeframe,
        side=row.side,
        units=float(row.units),
        entry_price=float(row.entry_price),
        entry_ts=row.entry_ts,
        stop_loss=_f(row.stop_loss),
        take_profit=_f(row.take_profit),
        status=row.status,
        exit_price=_f(row.exit_price),
        exit_ts=row.exit_ts,
        exit_reason=row.exit_reason,
        gross_pnl=_f(row.gross_pnl),
        net_pnl=_f(row.net_pnl),
    )


def _order(row: PaperOrderRow) -> PortfolioOrder:
    return PortfolioOrder(
        id=row.id,
        decision_id=row.decision_id,
        symbol=row.symbol,
        timeframe=row.timeframe,
        side=row.side,
        order_type=row.order_type,
        status=row.status,
        units=float(row.units),
        requested_price=_f(row.requested_price),
        filled_price=_f(row.filled_price),
        costs=float(row.costs),
        stop_loss=_f(row.stop_loss),
        take_profit=_f(row.take_profit),
        filled_at=row.filled_at,
        created_at=row.created_at,
    )


def _equity_point(row: AccountSnapshotRow) -> PortfolioEquityPoint:
    return PortfolioEquityPoint(
        ts=row.ts,
        cash=float(row.cash),
        equity=float(row.equity),
        open_pnl=float(row.open_pnl),
        realized_pnl=float(row.realized_pnl),
        peak_equity=float(row.peak_equity),
        drawdown_pct=float(row.drawdown_pct),
        margin_used=float(row.margin_used),
    )


def _empty_summary() -> PortfolioSummary:
    return PortfolioSummary(
        as_of=None,
        cash=0.0,
        equity=0.0,
        open_pnl=0.0,
        realized_pnl=0.0,
        peak_equity=0.0,
        drawdown_pct=0.0,
        margin_used=0.0,
        open_positions=0,
        open_orders=0,
    )


async def list_positions(
    session: AsyncSession,
    *,
    status: str | None = None,
    limit: int,
) -> list[PortfolioPosition]:
    """Paper positions (newest first), joined to each order's decision."""
    status = _checked_status(status, _POSITION_STATUSES, "position")
    stmt = (
        select(PaperPositionRow, PaperOrderRow.decision_id)
        .join(PaperOrderRow, PaperOrderRow.id == PaperPositionRow.order_id)
        .order_by(PaperPositionRow.entry_ts.desc())
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(PaperPositionRow.status == status)
    rows = (await session.execute(stmt)).all()
    return [_position(row[0], row[1]) for row in rows]


async def list_orders(
    session: AsyncSession,
    *,
    status: str | None = None,
    limit: int,
) -> list[PortfolioOrder]:
    """Paper orders (newest first)."""
    status = _checked_status(status, _ORDER_STATUSES, "order")
    stmt = select(PaperOrderRow).order_by(PaperOrderRow.created_at.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(PaperOrderRow.status == status)
    rows = (await session.execute(stmt)).scalars().all()
    return [_order(row) for row in rows]


async def load_equity_history(
    session: AsyncSession,
    *,
    limit: int,
) -> list[PortfolioEquityPoint]:
    """Most recent snapshots, returned oldest -> newest (chart order)."""
    stmt = select(AccountSnapshotRow).order_by(AccountSnapshotRow.ts.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [_equity_point(row) for row in reversed(rows)]


async def load_portfolio_summary(session: AsyncSession) -> PortfolioSummary:
    """Latest account snapshot plus open position/order counts."""
    snapshot = (
        (
            await session.execute(
                select(AccountSnapshotRow).order_by(AccountSnapshotRow.ts.desc()).limit(1)
            )
        )
        .scalars()
        .first()
    )
    if snapshot is None:
        return _empty_summary()

    open_positions = (
        await session.execute(
            select(func.count())
            .select_from(PaperPositionRow)
            .where(PaperPositionRow.status == PaperPositionStatus.OPEN.value)
        )
    ).scalar_one()
    open_orders = (
        await session.execute(
            select(func.count())
            .select_from(PaperOrderRow)
            .where(PaperOrderRow.status == PaperOrderStatus.PENDING.value)
        )
    ).scalar_one()

    return PortfolioSummary(
        as_of=snapshot.ts,
        cash=float(snapshot.cash),
        equity=float(snapshot.equity),
        open_pnl=float(snapshot.open_pnl),
        realized_pnl=float(snapshot.realized_pnl),
        peak_equity=float(snapshot.peak_equity),
        drawdown_pct=float(snapshot.drawdown_pct),
        margin_used=float(snapshot.margin_used),
        open_positions=int(open_positions),
        open_orders=int(open_orders),
    )
