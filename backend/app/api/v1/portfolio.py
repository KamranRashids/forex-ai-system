"""Portfolio read-only API (Phase 13, Phase B).

Read/list endpoints over the Phase A paper ledger: account summary, open/closed
positions, paper orders and equity-snapshot history. There are no write
endpoints. SAFE MODE: this API exposes paper-accounting records only; nothing
here can create an order, mutate trading state, or reach a broker.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DBSession
from app.data.portfolio_reader import (
    PortfolioEquityPoint,
    PortfolioOrder,
    PortfolioPosition,
    PortfolioSummary,
    list_orders,
    list_positions,
    load_equity_history,
    load_portfolio_summary,
)
from app.schemas.portfolio import (
    PortfolioEquityPointOut,
    PortfolioOrderOut,
    PortfolioPositionOut,
    PortfolioSummaryOut,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

POSITION_STATUS_PATTERN = "^(OPEN|CLOSED)$"
ORDER_STATUS_PATTERN = "^(PENDING|FILLED|CANCELLED|REJECTED)$"
POSITION_LIMIT: int = 100
ORDER_LIMIT: int = 50
EQUITY_LIMIT: int = 500
MAX_LIMIT: int = 1000


def _summary_out(summary: PortfolioSummary) -> PortfolioSummaryOut:
    return PortfolioSummaryOut(
        as_of=summary.as_of,
        cash=summary.cash,
        equity=summary.equity,
        open_pnl=summary.open_pnl,
        realized_pnl=summary.realized_pnl,
        peak_equity=summary.peak_equity,
        drawdown_pct=summary.drawdown_pct,
        margin_used=summary.margin_used,
        open_positions=summary.open_positions,
        open_orders=summary.open_orders,
    )


def _position_out(row: PortfolioPosition) -> PortfolioPositionOut:
    return PortfolioPositionOut(
        id=row.id,
        order_id=row.order_id,
        decision_id=row.decision_id,
        symbol=row.symbol,
        timeframe=row.timeframe,
        side=cast(Literal["LONG", "SHORT"], row.side),
        units=row.units,
        entry_price=row.entry_price,
        entry_ts=row.entry_ts,
        stop_loss=row.stop_loss,
        take_profit=row.take_profit,
        status=cast(Literal["OPEN", "CLOSED"], row.status),
        exit_price=row.exit_price,
        exit_ts=row.exit_ts,
        exit_reason=row.exit_reason,
        gross_pnl=row.gross_pnl,
        net_pnl=row.net_pnl,
    )


def _order_out(row: PortfolioOrder) -> PortfolioOrderOut:
    return PortfolioOrderOut(
        id=row.id,
        decision_id=row.decision_id,
        symbol=row.symbol,
        timeframe=row.timeframe,
        side=cast(Literal["LONG", "SHORT"], row.side),
        order_type=cast(Literal["NEXT_OPEN"], row.order_type),
        status=cast(Literal["PENDING", "FILLED", "CANCELLED", "REJECTED"], row.status),
        units=row.units,
        requested_price=row.requested_price,
        filled_price=row.filled_price,
        costs=row.costs,
        stop_loss=row.stop_loss,
        take_profit=row.take_profit,
        filled_at=row.filled_at,
        created_at=row.created_at,
    )


def _equity_out(point: PortfolioEquityPoint) -> PortfolioEquityPointOut:
    return PortfolioEquityPointOut(
        ts=point.ts,
        cash=point.cash,
        equity=point.equity,
        open_pnl=point.open_pnl,
        realized_pnl=point.realized_pnl,
        peak_equity=point.peak_equity,
        drawdown_pct=point.drawdown_pct,
        margin_used=point.margin_used,
    )


@router.get("/summary")
async def portfolio_summary(
    session: DBSession,
    current: CurrentUser,
) -> PortfolioSummaryOut:
    """Latest account snapshot + open counts (viewer+)."""
    summary = await load_portfolio_summary(session)
    return _summary_out(summary)


@router.get("/positions", response_model=list[PortfolioPositionOut])
async def portfolio_positions(
    session: DBSession,
    current: CurrentUser,
    status: Annotated[str | None, Query(pattern=POSITION_STATUS_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = POSITION_LIMIT,
) -> list[PortfolioPositionOut]:
    """Paper positions, newest first (viewer+)."""
    rows = await list_positions(session, status=status, limit=limit)
    return [_position_out(r) for r in rows]


@router.get("/orders", response_model=list[PortfolioOrderOut])
async def portfolio_orders(
    session: DBSession,
    current: CurrentUser,
    status: Annotated[str | None, Query(pattern=ORDER_STATUS_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = ORDER_LIMIT,
) -> list[PortfolioOrderOut]:
    """Paper orders, newest first (viewer+)."""
    rows = await list_orders(session, status=status, limit=limit)
    return [_order_out(r) for r in rows]


@router.get("/equity", response_model=list[PortfolioEquityPointOut])
async def portfolio_equity(
    session: DBSession,
    current: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = EQUITY_LIMIT,
) -> list[PortfolioEquityPointOut]:
    """Snapshot history, oldest first for the equity chart (viewer+)."""
    points = await load_equity_history(session, limit=limit)
    return [_equity_out(p) for p in points]
