"""Persisted alert events (Phase 8).

An ``AlertEvent`` is the durable, at-least-once-consumed record produced by the
observability sources (staleness monitor, risk brake, LLM budget breaker, and
orchestrator/system health) and appended to ``alerts.stream``. The ``alerts``
worker consumes that stream and persists to this table idempotently (an
``event_id`` unique key), optionally fanning out to connected WebSocket
clients.

Source events are cross-producer, let-through by the ``alerts`` worker and
replayed for catch-up. ``acknowledged_at``/``acknowledged_by`` are set only by
the explicit admin/ack endpoint and are *observability only* — acknowledging an
alert never affects trading, risk, or any control path.

SAFE MODE: alerts are strictly observe/read/acknowledge records. Nothing here
can place an order or mutate trading state.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AlertEvent(Base):
    __tablename__ = "alert_events"
    __table_args__ = (
        Index("ix_alert_events_occurred_at", "occurred_at"),
        Index("ix_alert_events_type_created", "event_type", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: Stable producer-supplied event identity used for at-least-once idempotency.
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    #: Priority bucket: info / warning / critical (derived at emit time).
    severity: Mapped[Literal["info", "warning", "critical"]] = mapped_column(
        String(16),
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical')", name="ck_alert_events_severity"
        ),
        nullable=False,
        default="info",
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(12), nullable=True)
    timeframe: Mapped[str | None] = mapped_column(String(8), nullable=True)
    #: Envelope producer + correlation id so related events can be grouped.
    producer: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: When the source event occurred (the stream/DB truth for ordering).
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


def severity_for(event_type: str) -> Literal["info", "warning", "critical"]:
    """Map an event type to a default severity bucket."""
    lowered = event_type.lower()
    if "critical" in lowered:
        return "critical"
    if (
        "breach" in lowered
        or "staleness" in lowered
        or "veto" in lowered
        or "blocked" in lowered
        or "budget" in lowered
        or "brake" in lowered
    ):
        return "warning"
    return "info"
