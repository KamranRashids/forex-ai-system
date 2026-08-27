"""Phase 5: orchestrator decisions, risk evaluations, and persistent risk state.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=12), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column("bucket_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "fused_direction",
            sa.String(length=8),
            sa.CheckConstraint(
                "fused_direction IN ('LONG', 'SHORT', 'FLAT')",
                name="ck_decisions_fused_direction",
            ),
            nullable=False,
        ),
        sa.Column("confidence", sa.Numeric(6, 4), nullable=False),
        sa.Column("agreement", sa.Numeric(6, 4), nullable=False),
        sa.Column(
            "status",
            sa.String(length=12),
            sa.CheckConstraint(
                "status IN ('ANALYSIS', 'PAPER', 'BLOCKED')",
                name="ck_decisions_status",
            ),
            nullable=False,
        ),
        sa.Column("veto_code", sa.String(length=32), nullable=True),
        sa.Column("veto_reason", sa.Text(), nullable=True),
        sa.Column("inputs_hash", sa.String(length=64), nullable=False),
        sa.Column("weights", postgresql.JSONB(), nullable=False),
        sa.Column("code_versions", postgresql.JSONB(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "decision_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("symbol", "timeframe", "bucket_ts", name="uq_decisions_identity"),
    )
    op.create_index(
        "ix_decisions_symbol_tf_dt", "decisions", ["symbol", "timeframe", "decision_at"]
    )

    op.create_table(
        "risk_evaluations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "decision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("decisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("symbol", sa.String(length=12), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column("bucket_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("position_size_units", sa.Numeric(20, 6), nullable=True),
        sa.Column("price", sa.Numeric(18, 8), nullable=True),
        sa.Column("atr", sa.Numeric(18, 8), nullable=True),
        sa.Column("stop_loss", sa.Numeric(18, 8), nullable=True),
        sa.Column("take_profit", sa.Numeric(18, 8), nullable=True),
        sa.Column("rr_ratio", sa.Numeric(8, 4), nullable=True),
        sa.Column("risk_pct_account", sa.Numeric(8, 4), nullable=True),
        sa.Column("exposure_ok", sa.Boolean(), nullable=False),
        sa.Column("correlation_ok", sa.Boolean(), nullable=False),
        sa.Column("daily_loss_ok", sa.Boolean(), nullable=False),
        sa.Column("drawdown_ok", sa.Boolean(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "symbol", "timeframe", "bucket_ts", name="uq_risk_evaluations_identity"
        ),
    )

    op.create_table(
        "risk_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("period_key", sa.String(length=64), nullable=False),
        sa.Column("realized_loss", sa.Numeric(18, 8), nullable=False),
        sa.Column("peak_equity", sa.Numeric(18, 8), nullable=False),
        sa.Column("max_drawdown", sa.Numeric(18, 8), nullable=False),
        sa.Column("exposure", sa.Numeric(20, 6), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("scope", "period_key", name="uq_risk_state_scope_period"),
    )


def downgrade() -> None:
    op.drop_table("risk_state")
    op.drop_table("risk_evaluations")
    op.drop_index("ix_decisions_symbol_tf_dt", table_name="decisions")
    op.drop_table("decisions")
