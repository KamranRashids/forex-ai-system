"""Phase 8: durable alert_events table for observability alerts.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column(
            "severity",
            sa.String(length=16),
            sa.CheckConstraint(
                "severity IN ('info', 'warning', 'critical')", name="ck_alert_events_severity"
            ),
            nullable=False,
            server_default="info",
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("symbol", sa.String(length=12), nullable=True),
        sa.Column("timeframe", sa.String(length=8), nullable=True),
        sa.Column("producer", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_alert_events_occurred_at", "alert_events", ["occurred_at"])
    op.create_index("ix_alert_events_type_created", "alert_events", ["event_type", "created_at"])
    op.create_index("ix_alert_events_event_id", "alert_events", ["event_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_alert_events_event_id", table_name="alert_events")
    op.drop_index("ix_alert_events_type_created", table_name="alert_events")
    op.drop_index("ix_alert_events_occurred_at", table_name="alert_events")
    op.drop_table("alert_events")
