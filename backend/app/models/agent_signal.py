"""Persisted agent outputs (full audit of every signal, IMPLEMENTATION_PLAN §9).

Idempotency: the unique key (agent_id, symbol, timeframe, bucket_ts) means a
redelivered bar never duplicates signals; ``run_id`` records which processing
batch first produced the row (first-writer-wins).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SignalDirection(enum.StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class AgentSignalRow(Base):
    __tablename__ = "agent_signals"
    __table_args__ = (
        UniqueConstraint(
            "agent_id",
            "symbol",
            "timeframe",
            "bucket_ts",
            name="uq_agent_signals_identity",
        ),
        Index("ix_agent_signals_symbol_tf_created", "symbol", "timeframe", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: Correlation id of the processing batch that first stored this row.
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    agent_id: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(12), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(4), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    bucket_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rationale: Mapped[str | None] = mapped_column(nullable=True)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
