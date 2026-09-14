"""Phase 13, Phase A: paper-trading ledger (orders, positions, account snapshots).

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-13

Adds the durable accounting surface for future SAFE MODE paper trading. These
tables are paper-accounting only (SAFE MODE preserved): nothing routes toward a
live brokerage, PAPER decisions are not wired to them automatically, and fill
math lives in the deterministic PaperBroker reused by backtests.

[0.1.0 -> 0008]
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orders_paper",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "decision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("decisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("symbol", sa.String(length=12), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column(
            "side",
            sa.String(length=8),
            sa.CheckConstraint("side IN ('LONG', 'SHORT')", name="ck_orders_paper_side"),
            nullable=False,
        ),
        sa.Column(
            "order_type",
            sa.String(length=16),
            sa.CheckConstraint("order_type IN ('NEXT_OPEN')", name="ck_orders_paper_order_type"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            sa.CheckConstraint(
                "status IN ('PENDING', 'FILLED', 'CANCELLED', 'REJECTED')",
                name="ck_orders_paper_status",
            ),
            nullable=False,
        ),
        sa.Column("units", sa.Numeric(20, 6), nullable=False),
        sa.Column("requested_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("filled_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("costs", sa.Numeric(20, 6), nullable=False),
        sa.Column("stop_loss", sa.Numeric(18, 8), nullable=True),
        sa.Column("take_profit", sa.Numeric(18, 8), nullable=True),
        sa.Column("seed", sa.Integer(), nullable=False),
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
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("decision_id", name="uq_orders_paper_decision_id"),
    )
    op.create_index("ix_orders_paper_status_created", "orders_paper", ["status", "created_at"])
    op.create_index("ix_orders_paper_symbol", "orders_paper", ["symbol"])

    op.create_table(
        "positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders_paper.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=12), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column(
            "side",
            sa.String(length=8),
            sa.CheckConstraint("side IN ('LONG', 'SHORT')", name="ck_positions_side"),
            nullable=False,
        ),
        sa.Column("units", sa.Numeric(20, 6), nullable=False),
        sa.Column("entry_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("entry_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stop_loss", sa.Numeric(18, 8), nullable=True),
        sa.Column("take_profit", sa.Numeric(18, 8), nullable=True),
        sa.Column("costs", sa.Numeric(20, 6), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            sa.CheckConstraint("status IN ('OPEN', 'CLOSED')", name="ck_positions_status"),
            nullable=False,
        ),
        sa.Column("exit_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("exit_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_reason", sa.String(length=16), nullable=True),
        sa.Column("gross_pnl", sa.Numeric(20, 6), nullable=True),
        sa.Column("net_pnl", sa.Numeric(20, 6), nullable=True),
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
    op.create_index("ix_positions_symbol", "positions", ["symbol"])
    op.create_index(
        "uq_positions_open_symbol",
        "positions",
        ["symbol"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )

    op.create_table(
        "account_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cash", sa.Numeric(20, 6), nullable=False),
        sa.Column("equity", sa.Numeric(20, 6), nullable=False),
        sa.Column("open_pnl", sa.Numeric(20, 6), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(20, 6), nullable=False),
        sa.Column("peak_equity", sa.Numeric(20, 6), nullable=False),
        sa.Column("drawdown_pct", sa.Numeric(12, 8), nullable=False),
        sa.Column("margin_used", sa.Numeric(20, 6), nullable=False),
        sa.Column("extra", postgresql.JSONB(), nullable=False),
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
    op.create_index("ix_account_snapshots_ts", "account_snapshots", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_account_snapshots_ts", table_name="account_snapshots")
    op.drop_table("account_snapshots")
    op.drop_index("uq_positions_open_symbol", table_name="positions")
    op.drop_index("ix_positions_symbol", table_name="positions")
    op.drop_table("positions")
    op.drop_index("ix_orders_paper_symbol", table_name="orders_paper")
    op.drop_index("ix_orders_paper_status_created", table_name="orders_paper")
    op.drop_table("orders_paper")
    # decisions / risk_* tables are untouched (unchanged by 0008).
