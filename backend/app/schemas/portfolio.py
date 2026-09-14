"""Portfolio read API schemas (Phase 13, Phase B).

Read-only views over the Phase A paper ledger (``orders_paper``,
``positions``, ``account_snapshots``). SAFE MODE: these schemas describe
paper-accounting records only; nothing here can create an order or reach a
broker.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class PortfolioSummaryOut(BaseModel):
    """Latest paper-account snapshot plus open position/order counts."""

    model_config = ConfigDict(from_attributes=True)

    as_of: datetime | None = None
    cash: float = 0.0
    equity: float = 0.0
    open_pnl: float = 0.0
    realized_pnl: float = 0.0
    peak_equity: float = 0.0
    drawdown_pct: float = 0.0
    margin_used: float = 0.0
    open_positions: int = 0
    open_orders: int = 0


class PortfolioPositionOut(BaseModel):
    """One simulated paper position (joined to its originating decision)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_id: uuid.UUID
    decision_id: uuid.UUID | None = None
    symbol: str
    timeframe: str
    side: Literal["LONG", "SHORT"]
    units: float
    entry_price: float
    entry_ts: datetime
    stop_loss: float | None = None
    take_profit: float | None = None
    status: Literal["OPEN", "CLOSED"]
    exit_price: float | None = None
    exit_ts: datetime | None = None
    exit_reason: str | None = None
    gross_pnl: float | None = None
    net_pnl: float | None = None


class PortfolioOrderOut(BaseModel):
    """One paper order against the ledger."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    decision_id: uuid.UUID | None = None
    symbol: str
    timeframe: str
    side: Literal["LONG", "SHORT"]
    order_type: Literal["NEXT_OPEN"]
    status: Literal["PENDING", "FILLED", "CANCELLED", "REJECTED"]
    units: float
    requested_price: float | None = None
    filled_price: float | None = None
    costs: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    filled_at: datetime | None = None
    created_at: datetime


class PortfolioEquityPointOut(BaseModel):
    """Point-in-time account snapshot (chronological order for charts)."""

    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    cash: float
    equity: float
    open_pnl: float
    realized_pnl: float
    peak_equity: float
    drawdown_pct: float
    margin_used: float
