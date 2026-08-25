"""Phase 2 baseline: instruments, candles, provider_health.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("symbol", sa.String(length=12), nullable=False),
        sa.Column("base", sa.String(length=8), nullable=False),
        sa.Column("quote", sa.String(length=8), nullable=False),
        sa.Column("pip_size", sa.Numeric(12, 8), nullable=False),
        sa.Column("price_decimals", sa.SmallInteger(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
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
            nullable=False,
        ),
    )
    op.create_index("ix_instruments_symbol", "instruments", ["symbol"], unique=True)

    op.create_table(
        "candles",
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "instruments.id",
                ondelete="CASCADE",
                name="fk_candles_instrument_id_instruments",
            ),
            primary_key=True,
        ),
        sa.Column("timeframe", sa.String(length=4), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("open", sa.Numeric(18, 8), nullable=False),
        sa.Column("high", sa.Numeric(18, 8), nullable=False),
        sa.Column("low", sa.Numeric(18, 8), nullable=False),
        sa.Column("close", sa.Numeric(18, 8), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("tf_minutes", sa.SmallInteger(), nullable=False),
    )

    op.create_table(
        "provider_health",
        sa.Column("provider", sa.String(length=32), primary_key=True),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("breaker_state", sa.String(length=12), nullable=False),
        sa.Column("breaker_opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("provider_health")
    op.drop_table("candles")
    op.drop_index("ix_instruments_symbol", table_name="instruments")
    op.drop_table("instruments")
