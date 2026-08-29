"""Backtest read-only API (Phase 6).

Only read/list endpoints — there is no backtest execution endpoint here (that
is the CLI). SAFE MODE: this API exposes analysis records only; nothing here can
create an order or touch live/paper trading state.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import CurrentUser, DBSession
from app.core.errors import NotFoundError
from app.models.backtest import BacktestEquityRow, BacktestRunRow, BacktestTradeRow
from app.schemas.backtests import (
    BacktestEquityOut,
    BacktestRunOut,
    BacktestTradeOut,
)

router = APIRouter(prefix="/backtests", tags=["backtests"])


def _run_out(row: BacktestRunRow) -> BacktestRunOut:
    return BacktestRunOut(
        id=row.id,
        status=cast(Literal["RUNNING", "COMPLETED", "FAILED"], row.status),
        metrics=row.metrics or {},
        error=row.error,
        seed=row.seed,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


@router.get("")
async def list_backtests(
    session: DBSession,
    current: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[BacktestRunOut]:
    """Recent backtest runs (viewer+)."""
    rows = (
        (
            await session.execute(
                select(BacktestRunRow).order_by(BacktestRunRow.started_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_run_out(r) for r in rows]


@router.get("/{run_id}")
async def get_backtest(
    session: DBSession,
    current: CurrentUser,
    run_id: uuid.UUID,
) -> BacktestRunOut:
    """Detail (incl. metrics) for one run (viewer+)."""
    row = await session.get(BacktestRunRow, run_id)
    if row is None:
        raise NotFoundError(f"backtest run {run_id} not found")
    return _run_out(row)


@router.get("/{run_id}/trades")
async def backtest_trades(
    session: DBSession,
    current: CurrentUser,
    run_id: uuid.UUID,
) -> list[BacktestTradeOut]:
    """Simulated fills for a run (viewer+)."""
    rows = (
        (await session.execute(select(BacktestTradeRow).where(BacktestTradeRow.run_id == run_id)))
        .scalars()
        .all()
    )
    return [_trade(r) for r in rows]


@router.get("/{run_id}/equity")
async def backtest_equity(
    session: DBSession,
    current: CurrentUser,
    run_id: uuid.UUID,
) -> list[BacktestEquityOut]:
    """Equity curve for a run (viewer+)."""
    rows = (
        (
            await session.execute(
                select(BacktestEquityRow)
                .where(BacktestEquityRow.run_id == run_id)
                .order_by(BacktestEquityRow.ts)
            )
        )
        .scalars()
        .all()
    )
    return [
        BacktestEquityOut(
            id=r.id,
            ts=r.ts,
            equity=float(r.equity),
            drawdown_pct=float(r.drawdown_pct),
        )
        for r in rows
    ]


def _trade(row: BacktestTradeRow) -> BacktestTradeOut:
    return BacktestTradeOut(
        id=row.id,
        symbol=row.symbol,
        timeframe=row.timeframe,
        side=cast(Literal["LONG", "SHORT"], row.side),
        units=float(row.units),
        entry_ts=row.entry_ts,
        entry_price=float(row.entry_price),
        exit_ts=row.exit_ts,
        exit_price=float(row.exit_price),
        gross_pnl=float(row.gross_pnl),
        costs=float(row.costs),
        net_pnl=float(row.net_pnl),
        exit_reason=row.exit_reason,
    )
