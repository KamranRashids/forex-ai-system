"""Persisted paper-trading ledger (Phase 13, Phase A).

These tables are the durable accounting surface for future SAFE MODE paper
trading: paper orders, one open position per symbol, and account equity
snapshots. They exist so a paper executor (future phase) can trade the full
simulated lifecycle (order -> fill -> position -> exit -> snapshot) and be
replayed/restored deterministically.

SAFE MODE: this is *paper accounting only*. Nothing here can route, place, or
simulate toward a live brokerage order; there is no executor wiring anywhere in
this module, and nothing automatically subscribes PAPER decisions to these
tables yet. ``LedgerBroker`` (app/broker/ledger.py) fills against the same
deterministic ``PaperBroker`` math used by backtests.

[0.1.0 -> 0008]
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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PaperOrderType(enum.StrEnum):
    """Fill policy attached to an order (only one type exists in Phase A)."""

    NEXT_OPEN = "NEXT_OPEN"


class PaperOrderStatus(enum.StrEnum):
    """Lifecycle of one paper order."""

    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class PaperPositionStatus(enum.StrEnum):
    """Lifecycle of one simulated paper position."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


class PaperOrderRow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One order against the paper ledger (fill semantics in LedgerBroker)."""

    __tablename__ = "orders_paper"
    __table_args__ = (
        CheckConstraint("side IN ('LONG', 'SHORT')", name="ck_orders_paper_side"),
        CheckConstraint("order_type IN ('NEXT_OPEN')", name="ck_orders_paper_order_type"),
        CheckConstraint(
            "status IN ('PENDING', 'FILLED', 'CANCELLED', 'REJECTED')",
            name="ck_orders_paper_status",
        ),
        #: At most one order may originate from a single (non-executing) decision.
        UniqueConstraint("decision_id", name="uq_orders_paper_decision_id"),
        Index("ix_orders_paper_status_created", "status", "created_at"),
        Index("ix_orders_paper_symbol", "symbol"),
    )

    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("decisions.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(12), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(4), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PaperOrderType.NEXT_OPEN.value
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PaperOrderStatus.PENDING.value
    )
    units: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    requested_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    filled_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    costs: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, default=Decimal("0"))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    #: Determinism seed reused when simulating entry/exit costs.
    seed: Mapped[int] = mapped_column(nullable=False, default=0)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaperPositionRow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One simulated paper position; at most one OPEN per symbol."""

    __tablename__ = "positions"
    __table_args__ = (
        CheckConstraint("side IN ('LONG', 'SHORT')", name="ck_positions_side"),
        CheckConstraint("status IN ('OPEN', 'CLOSED')", name="ck_positions_status"),
        Index("ix_positions_symbol", "symbol"),
        #: One open paper position per symbol (PaperBroker semantics superset).
        Index(
            "uq_positions_open_symbol",
            "symbol",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders_paper.id", ondelete="RESTRICT"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(12), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(4), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    units: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    entry_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    costs: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, default=Decimal("0"))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PaperPositionStatus.OPEN.value
    )
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Closed positions carry full round-trip values (entry + exit costs netted).
    gross_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    net_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)


class AccountSnapshotRow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Point-in-time account equity snapshot (the persistent BrokerState)."""

    __tablename__ = "account_snapshots"
    __table_args__ = (Index("ix_account_snapshots_ts", "ts"),)

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    open_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, default=Decimal("0")
    )
    peak_equity: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    drawdown_pct: Mapped[Decimal] = mapped_column(
        Numeric(12, 8), nullable=False, default=Decimal("0")
    )
    margin_used: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, default=Decimal("0")
    )
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
