"""Phase 6: backtest runs, simulated trades, and equity curves.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-27

These tables are analysis-only (SAFE MODE). Backtests never create live orders
and never touch orders_paper/positions.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("data_range", postgresql.JSONB(), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("code_versions", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            sa.CheckConstraint(
                "status IN ('RUNNING', 'COMPLETED', 'FAILED')",
                name="ck_backtest_runs_status",
            ),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_backtest_runs_created", "backtest_runs", ["created_at"])

    op.create_table(
        "backtest_trades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=12), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column(
            "side",
            sa.String(length=8),
            sa.CheckConstraint("side IN ('LONG', 'SHORT')", name="ck_backtest_trades_side"),
            nullable=False,
        ),
        sa.Column("units", sa.Numeric(20, 6), nullable=False),
        sa.Column("entry_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("exit_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("gross_pnl", sa.Numeric(20, 6), nullable=False),
        sa.Column("costs", sa.Numeric(20, 6), nullable=False),
        sa.Column("net_pnl", sa.Numeric(20, 6), nullable=False),
        sa.Column("exit_reason", sa.String(length=16), nullable=False),
    )
    op.create_index("ix_backtest_trades_run_symbol", "backtest_trades", ["run_id", "symbol"])

    op.create_table(
        "backtest_equity",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("equity", sa.Numeric(20, 6), nullable=False),
        sa.Column("drawdown_pct", sa.Numeric(12, 8), nullable=False),
        sa.UniqueConstraint("run_id", "ts", name="uq_backtest_equity_run_ts"),
    )


def downgrade() -> None:
    op.drop_table("backtest_equity")
    op.drop_table("backtest_trades")
    op.drop_index("ix_backtest_runs_created", table_name="backtest_runs")
    op.drop_table("backtest_runs")
