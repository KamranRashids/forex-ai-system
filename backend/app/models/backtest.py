"""Persisted backtest runs, simulated trades, and equity curves (Phase 6).

SAFE MODE: these tables are *analysis only*. A backtest never creates live
orders and never touches ``orders_paper`` or ``positions``; it records its
simulated paper fills in ``backtest_trades`` and the resulting equity in
``backtest_equity``. Nothing here can be consumed by a live executor.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BacktestStatus(enum.StrEnum):
    """Lifecycle of a backtest run."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Side(enum.StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_backtest_runs_status",
        ),
        Index("ix_backtest_runs_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: Frozen reproduction input set (symbols, timeframes, range, seed, params).
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    data_range: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    seed: Mapped[int] = mapped_column(nullable=False, default=0)
    #: agent_id -> version seen, plus engine/backtest module versions.
    code_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=BacktestStatus.RUNNING.value
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Final aggregate metrics (net/gross pnl, win-rate, sharpe, ... ).
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BacktestTradeRow(Base):
    __tablename__ = "backtest_trades"
    __table_args__ = (Index("ix_backtest_trades_run_symbol", "run_id", "symbol"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(12), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(4), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    units: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    entry_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    exit_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    costs: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, default=Decimal("0"))
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    exit_reason: Mapped[str] = mapped_column(String(16), nullable=False, default="signal")


class BacktestEquityRow(Base):
    __tablename__ = "backtest_equity"
    __table_args__ = (UniqueConstraint("run_id", "ts", name="uq_backtest_equity_run_ts"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    drawdown_pct: Mapped[Decimal] = mapped_column(
        Numeric(12, 8), nullable=False, default=Decimal("0")
    )
