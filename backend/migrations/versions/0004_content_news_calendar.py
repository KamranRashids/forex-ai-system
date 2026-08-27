"""Phase 4: normalized news + economic-calendar tables with dedup keys.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "economic_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dedup_key", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("symbols", postgresql.ARRAY(sa.String(length=12)), nullable=False),
        sa.Column(
            "importance",
            sa.String(length=8),
            sa.CheckConstraint(
                "importance IN ('low', 'medium', 'high')",
                name="ck_economic_events_importance",
            ),
            nullable=False,
        ),
        sa.Column("timestamp_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actual", sa.Text(), nullable=True),
        sa.Column("forecast", sa.Text(), nullable=True),
        sa.Column("previous", sa.Text(), nullable=True),
        sa.Column("surprise_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
            nullable=True,
        ),
        sa.UniqueConstraint("dedup_key", name="uq_economic_events_dedup"),
    )
    op.create_index("ix_economic_events_ts", "economic_events", ["timestamp_utc"])
    op.create_index(
        "ix_economic_events_currency_ts", "economic_events", ["currency", "timestamp_utc"]
    )

    op.create_table(
        "news_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("item_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("symbols", postgresql.ARRAY(sa.String(length=12)), nullable=False),
        sa.Column("published_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("item_hash", name="uq_news_items_hash"),
    )
    op.create_index("ix_news_items_published", "news_items", ["published_utc"])
    op.create_index("ix_news_items_symbols_published", "news_items", ["symbols", "published_utc"])


def downgrade() -> None:
    op.drop_index("ix_news_items_symbols_published", table_name="news_items")
    op.drop_index("ix_news_items_published", table_name="news_items")
    op.drop_table("news_items")
    op.drop_index("ix_economic_events_currency_ts", table_name="economic_events")
    op.drop_index("ix_economic_events_ts", table_name="economic_events")
    op.drop_table("economic_events")
