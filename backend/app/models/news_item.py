"""Persisted, normalized news items (Phase 4).

Idempotency: ``item_hash`` is a deterministic hash of stable,
provider-independent fields (never ingestion time), so a repolled/redelivered
story never duplicates. ``published_utc`` is stored separately from
``created_at``; ``raw_payload`` is a sanitized provider snapshot kept for
audit/debug — provider credentials are never stored.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (
        UniqueConstraint("item_hash", name="uq_news_items_hash"),
        Index("ix_news_items_published", "published_utc"),
        Index("ix_news_items_symbols_published", "symbols", "published_utc"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    item_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    symbols: Mapped[list[str]] = mapped_column(ARRAY(String(12)), nullable=False, default=list)
    published_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
