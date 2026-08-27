"""Persisted orchestrator decisions (Phase 5).

A ``Decision`` is the durable, auditable output of the decision pipeline for
one closed bar of one (symbol, timeframe). It records the fused direction,
confidence, agreement, the per-agent weights and code versions used, an
``inputs_hash`` for deterministic reproduction, and the risk outcome
(status + veto reason when blocked).

SAFE MODE (L3): a decision is *analysis / paper intent only*. There is no
order, broker, or execution capability anywhere in this module or its
consumers. Status values are ANALYSIS / PAPER / BLOCKED — no live-order shape
exists.

Idempotency: the unique key (symbol, timeframe, bucket_ts) means a replayed or
redelivered bar never produces a duplicate decision; ``run_id`` records the
processing batch that first stored the row (first-writer-wins).
"""

from __future__ import annotations

import enum
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DecisionStatus(enum.StrEnum):
    """Lifecycle of a decision.

    - ANALYSIS: a decision was formed but did not meet the bar to act on
      (e.g. insufficient agreement/coverage) — informational only.
    - PAPER: a paper intent is produced and fully risk-validated.
    - BLOCKED: risk controls vetoed (fail closed) — never actionable.
    """

    ANALYSIS = "ANALYSIS"
    PAPER = "PAPER"
    BLOCKED = "BLOCKED"


class DecisionDirection(enum.StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class DecisionRow(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "timeframe",
            "bucket_ts",
            name="uq_decisions_identity",
        ),
        Index("ix_decisions_symbol_tf_dt", "symbol", "timeframe", "decision_at"),
        CheckConstraint(
            "status IN ('ANALYSIS', 'PAPER', 'BLOCKED')",
            name="ck_decisions_status",
        ),
        CheckConstraint(
            "fused_direction IN ('LONG', 'SHORT', 'FLAT')",
            name="ck_decisions_fused_direction",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    symbol: Mapped[str] = mapped_column(String(12), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(4), nullable=False)
    bucket_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fused_direction: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    agreement: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    #: Machine code of the decisive veto when BLOCKED, e.g. "min_rr".
    veto_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    veto_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Deterministic hash of the input signal identities/versions + bucket.
    inputs_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: Per-agent fusion weights applied (frozen for auditability).
    weights: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: agent_id -> version seen (frozen for auditability).
    code_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
