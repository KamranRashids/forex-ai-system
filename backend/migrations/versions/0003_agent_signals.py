"""Phase 3: agent_signals table with idempotent identity key.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("agent_version", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=12), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column(
            "direction",
            sa.String(length=8),
            sa.CheckConstraint(
                "direction IN ('LONG', 'SHORT', 'FLAT')", name="ck_agent_signals_direction"
            ),
            nullable=False,
        ),
        sa.Column("confidence", sa.Numeric(6, 4), nullable=False),
        sa.Column("bucket_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("features", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "agent_id",
            "symbol",
            "timeframe",
            "bucket_ts",
            name="uq_agent_signals_identity",
        ),
    )
    op.create_index(
        "ix_agent_signals_symbol_tf_created",
        "agent_signals",
        ["symbol", "timeframe", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_signals_symbol_tf_created", table_name="agent_signals")
    op.drop_table("agent_signals")
