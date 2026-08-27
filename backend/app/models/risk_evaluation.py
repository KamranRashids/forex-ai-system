"""Auditable risk-evaluation snapshot (Phase 5).

Every risk pass for a (symbol, timeframe, bucket_ts) is persisted — even when
the decision was blocked — so risk controls are auditable and reproducible.
A row holds the computed position sizing, stop-loss/take-profit and reward:risk,
plus the outcome of each independent gate.

SAFE MODE: purely descriptive paper-sizing math. Nothing here can place or
route an order; position size is a paper figure only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RiskEvaluationRow(Base):
    __tablename__ = "risk_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "timeframe",
            "bucket_ts",
            name="uq_risk_evaluations_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("decisions.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(12), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(4), nullable=False)
    bucket_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # --- Paper sizing -------------------------------------------------------
    position_size_units: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    #: Reference price at evaluation time (notional = units * price).
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    #: ATR(14) used for sizing (audit + replay).
    atr: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    rr_ratio: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    risk_pct_account: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)

    # --- Independent gates (fail closed) ------------------------------------
    exposure_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    correlation_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    daily_loss_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    drawdown_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: True when EVERY gate passed and a paper intent may be emitted.
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Structured list of {"code", "ok", "detail"} per gate.
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
