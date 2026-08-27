"""Persisted, normalized economic-calendar events (Phase 4).

Idempotency: ``dedup_key`` is a deterministic hash of stable fields (or the
provider's external id) so a repolled/redelivered event never duplicates
rows. ``raw_payload`` is a sanitized provider snapshot kept for audit/debug —
provider credentials are never stored.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EconomicEvent(Base):
    __tablename__ = "economic_events"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_economic_events_dedup"),
        Index("ix_economic_events_ts", "timestamp_utc"),
        Index("ix_economic_events_currency_ts", "currency", "timestamp_utc"),
        CheckConstraint(
            "importance IN ('low', 'medium', 'high')",
            name="ck_economic_events_importance",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    symbols: Mapped[list[str]] = mapped_column(ARRAY(String(12)), nullable=False, default=list)
    importance: Mapped[str] = mapped_column(String(8), nullable=False)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actual: Mapped[str | None] = mapped_column(Text, nullable=True)
    forecast: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous: Mapped[str | None] = mapped_column(Text, nullable=True)
    surprise_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()"), nullable=True
    )
