"""Backtest persistence: run header, trades, and equity curve (Phase 6).

Persists only to ``backtest_*`` tables (analysis-only). A backtest never
inserts into ``orders_paper`` / ``positions`` and never creates a live order.

SAFE MODE: these writes are durable analysis records consumed by the read-only
backtest API/CLI; no executor reads them to place an order.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.backtest.models import BacktestConfig, RunMetrics
from app.broker.paper import Trade
from app.models.backtest import (
    BacktestEquityRow,
    BacktestRunRow,
    BacktestStatus,
    BacktestTradeRow,
)


async def create_run(
    session: AsyncSession,
    *,
    cfg: BacktestConfig,
    code_versions: dict[str, str],
) -> uuid.UUID:
    row = BacktestRunRow(
        id=uuid.uuid4(),
        config=cfg.to_jsonable(),
        data_range={"start": cfg.start.isoformat(), "end": cfg.end.isoformat()},
        seed=cfg.seed,
        code_versions=code_versions,
        status=BacktestStatus.RUNNING.value,
        metrics={},
    )
    session.add(row)
    await session.flush()
    return row.id


async def mark_completed(session: AsyncSession, *, run_id: uuid.UUID, metrics: RunMetrics) -> None:
    row = await session.get(BacktestRunRow, run_id)
    if row is None:
        return
    row.status = BacktestStatus.COMPLETED.value
    row.metrics = metrics.to_dict()
    row.finished_at = datetime.now(UTC)
    await session.flush()


async def mark_failed(session: AsyncSession, *, run_id: uuid.UUID, error: str) -> None:
    row = await session.get(BacktestRunRow, run_id)
    if row is None:
        return
    row.status = BacktestStatus.FAILED.value
    row.error = error
    row.finished_at = datetime.now(UTC)
    await session.flush()


async def save_trades(session: AsyncSession, *, run_id: uuid.UUID, trades: list[Trade]) -> None:
    for trade in trades:
        session.add(
            BacktestTradeRow(
                run_id=run_id,
                symbol=trade.symbol,
                timeframe=trade.timeframe,
                side=trade.side.value,
                units=Decimal(str(trade.units)),
                entry_ts=trade.entry_ts,
                entry_price=Decimal(str(trade.entry_price)),
                exit_ts=trade.exit_ts,
                exit_price=Decimal(str(trade.exit_price)),
                gross_pnl=Decimal(str(trade.gross_pnl)),
                costs=Decimal(str(trade.costs)),
                net_pnl=Decimal(str(trade.net_pnl)),
                exit_reason=trade.exit_reason,
            )
        )
    if trades:
        await session.flush()


async def save_equity_curve(
    session: AsyncSession, *, run_id: uuid.UUID, points: list[tuple[datetime, float]]
) -> None:
    for ts, equity in points:
        session.add(
            BacktestEquityRow(
                run_id=run_id, ts=ts, equity=Decimal(str(equity)), drawdown_pct=Decimal("0")
            )
        )
    if points:
        await session.flush()


async def list_runs(session: AsyncSession, *, limit: int = 20) -> list[BacktestRunRow]:
    result = await session.execute(
        select(BacktestRunRow).order_by(BacktestRunRow.started_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> BacktestRunRow | None:
    return await session.get(BacktestRunRow, run_id)


async def get_trades(session: AsyncSession, run_id: uuid.UUID) -> list[BacktestTradeRow]:
    result = await session.execute(
        select(BacktestTradeRow).where(BacktestTradeRow.run_id == run_id)
    )
    return list(result.scalars().all())


async def get_equity(session: AsyncSession, run_id: uuid.UUID) -> list[BacktestEquityRow]:
    result = await session.execute(
        select(BacktestEquityRow)
        .where(BacktestEquityRow.run_id == run_id)
        .order_by(BacktestEquityRow.ts)
    )
    return list(result.scalars().all())
