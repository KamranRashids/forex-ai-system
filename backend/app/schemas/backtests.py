"""Backtest read API schemas (Phase 6)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class BacktestRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: Literal["RUNNING", "COMPLETED", "FAILED"]
    metrics: dict[str, Any]
    error: str | None = None
    seed: int
    started_at: datetime
    finished_at: datetime | None = None


class BacktestTradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    symbol: str
    timeframe: str
    side: Literal["LONG", "SHORT"]
    units: float
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    gross_pnl: float
    costs: float
    net_pnl: float
    exit_reason: str


class BacktestEquityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ts: datetime
    equity: float
    drawdown_pct: float
