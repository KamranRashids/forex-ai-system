"""Persistent rolling risk counters (Phase 5).

Brakes must survive worker restarts, so aggregate state (realized loss, peak
equity, max drawdown, open paper exposure) is stored here keyed by
``(scope, period_key)`` — e.g. scope=``daily``/period_key=``YYYY-MM-DD`` or
scope=``account``/period_key=``global``.

SAFE MODE: pure bookkeeping of paper/analysis risk; no order path.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RiskStateRow(Base):
    __tablename__ = "risk_state"
    __table_args__ = (UniqueConstraint("scope", "period_key", name="uq_risk_state_scope_period"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    period_key: Mapped[str] = mapped_column(String(64), nullable=False)
    realized_loss: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=0)
    peak_equity: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=0)
    max_drawdown: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=0)
    #: Current open paper exposure (notional units), recomputed by the worker.
    exposure: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
