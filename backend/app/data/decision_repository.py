"""Decision + risk persistence (idempotent, first-writer-wins).

Mirrors ``signal_repository``: the unique key (symbol, timeframe, bucket_ts)
guarantees a replayed or redelivered bar never creates a duplicate decision or
risk evaluation. Aggregate ``risk_state`` rows are upserted (they are mutable
accumulators, not immutable events).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.decisions.fusion import FusedResult
from app.decisions.risk import RiskOutcome
from app.models.decision import DecisionRow, DecisionStatus
from app.models.risk_evaluation import RiskEvaluationRow
from app.models.risk_state import RiskStateRow


@dataclass(frozen=True, slots=True)
class PaperSnapshot:
    """One active (still-valid) PAPER decision's exposure contribution."""

    symbol: str
    notional: float
    basket: frozenset[str]


def decision_values(
    *,
    run_id: str,
    symbol: str,
    timeframe: str,
    bucket_ts: datetime,
    fused: FusedResult,
    status: DecisionStatus,
    veto_code: str | None,
    veto_reason: str | None,
    inputs_hash: str,
    code_versions: dict[str, str],
    rationale: str | None,
    valid_until: datetime | None,
) -> dict[str, object]:
    return {
        "id": uuid.uuid4(),
        "run_id": run_id,
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "bucket_ts": bucket_ts,
        "fused_direction": fused.direction.value,
        "confidence": round(fused.confidence, 4),
        "agreement": round(fused.agreement, 4),
        "status": status.value,
        "veto_code": veto_code,
        "veto_reason": veto_reason,
        "inputs_hash": inputs_hash,
        "weights": fused.weights,
        "code_versions": code_versions,
        "rationale": rationale,
        "valid_until": valid_until,
    }


async def save_decision(session: AsyncSession, values: dict[str, object]) -> bool:
    """Idempotent insert. Returns True when this writer stored the row."""
    stmt = (
        pg_insert(DecisionRow)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["symbol", "timeframe", "bucket_ts"])
        .returning(DecisionRow.id)
    )
    return (await session.scalar(stmt)) is not None


async def save_risk_evaluation(
    session: AsyncSession,
    *,
    decision_id: uuid.UUID | None,
    symbol: str,
    timeframe: str,
    bucket_ts: datetime,
    outcome: RiskOutcome,
) -> bool:
    """Idempotent insert; the risk snapshot for a key is stored once."""
    stmt = (
        pg_insert(RiskEvaluationRow)
        .values(
            id=uuid.uuid4(),
            decision_id=decision_id,
            symbol=symbol.upper(),
            timeframe=timeframe,
            bucket_ts=bucket_ts,
            position_size_units=outcome.sizing.position_size_units if outcome.sizing else None,
            price=outcome.sizing.price if outcome.sizing else None,
            atr=outcome.sizing.atr if outcome.sizing else None,
            stop_loss=outcome.sizing.stop_loss if outcome.sizing else None,
            take_profit=outcome.sizing.take_profit if outcome.sizing else None,
            rr_ratio=outcome.sizing.rr_ratio if outcome.sizing else None,
            risk_pct_account=outcome.sizing.risk_pct_account if outcome.sizing else None,
            exposure_ok=outcome.gates["exposure"].ok,
            correlation_ok=outcome.gates["correlation"].ok,
            daily_loss_ok=outcome.gates["daily_loss"].ok,
            drawdown_ok=outcome.gates["drawdown"].ok,
            passed=outcome.passed,
            reasons=outcome.reasons,
        )
        .on_conflict_do_nothing(index_elements=["symbol", "timeframe", "bucket_ts"])
        .returning(RiskEvaluationRow.id)
    )
    return (await session.scalar(stmt)) is not None


async def upsert_risk_state(
    session: AsyncSession,
    *,
    scope: str,
    period_key: str,
    realized_loss: float | None = None,
    peak_equity: float | None = None,
    max_drawdown: float | None = None,
    exposure: float | None = None,
    now: datetime | None = None,
) -> None:
    """Accumulate a mutable risk counter row for (scope, period_key)."""
    stmt = pg_insert(RiskStateRow).values(
        id=uuid.uuid4(),
        scope=scope,
        period_key=period_key,
        realized_loss=realized_loss if realized_loss is not None else 0,
        peak_equity=peak_equity if peak_equity is not None else 0,
        max_drawdown=max_drawdown if max_drawdown is not None else 0,
        exposure=exposure if exposure is not None else 0,
    )
    updates: dict[str, object] = {"scope": stmt.excluded.scope}
    if realized_loss is not None:
        updates["realized_loss"] = stmt.excluded.realized_loss
    if peak_equity is not None:
        updates["peak_equity"] = stmt.excluded.peak_equity
    if max_drawdown is not None:
        updates["max_drawdown"] = stmt.excluded.max_drawdown
    if exposure is not None:
        updates["exposure"] = stmt.excluded.exposure
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["scope", "period_key"],
            set_=updates,
        )
    )


async def load_latest_decision(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    now: datetime | None = None,
) -> DecisionRow | None:
    """Newest decision row for a (symbol, timeframe), optionally fresh-only."""
    conditions = [
        DecisionRow.symbol == symbol.upper(),
        DecisionRow.timeframe == timeframe,
    ]
    if now is not None:
        conditions.append((DecisionRow.valid_until.is_(None)) | (DecisionRow.valid_until >= now))
    result = await session.execute(
        select(DecisionRow).where(*conditions).order_by(DecisionRow.decision_at.desc()).limit(1)
    )
    return result.scalars().first()


async def load_decisions(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    limit: int = 50,
    order: str = "desc",
) -> list[DecisionRow]:
    """Decision history for a pair/timeframe (newest first unless ordered asc)."""
    direction = DecisionRow.decision_at.desc() if order == "desc" else DecisionRow.decision_at.asc()
    result = await session.execute(
        select(DecisionRow)
        .where(DecisionRow.symbol == symbol.upper(), DecisionRow.timeframe == timeframe)
        .order_by(direction)
        .limit(limit)
    )
    return list(result.scalars().all())


async def load_risk_evaluations(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    limit: int = 50,
) -> list[RiskEvaluationRow]:
    result = await session.execute(
        select(RiskEvaluationRow)
        .where(
            RiskEvaluationRow.symbol == symbol.upper(),
            RiskEvaluationRow.timeframe == timeframe,
        )
        .order_by(RiskEvaluationRow.evaluated_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def load_risk_state(
    session: AsyncSession, *, scope: str, period_key: str
) -> RiskStateRow | None:
    result = await session.execute(
        select(RiskStateRow).where(
            RiskStateRow.scope == scope, RiskStateRow.period_key == period_key
        )
    )
    return result.scalars().first()


async def load_active_paper_snapshot(
    session: AsyncSession, *, now: datetime, symbol: str | None = None
) -> list[PaperSnapshot]:
    """Active (still valid) PAPER decisions with their exposure contribution.

    Returns a :class:`PaperSnapshot` per active decision (oldest first) with its
    notional (units * price) and currency basket, used to compute total and
    per-basket exposure without a separate position ledger (that arrives with
    the Phase 6 paper executor).
    """
    conditions = [
        DecisionRow.status == DecisionStatus.PAPER.value,
        DecisionRow.valid_until.isnot(None),
        DecisionRow.valid_until >= now,
        RiskEvaluationRow.position_size_units.isnot(None),
        RiskEvaluationRow.price.isnot(None),
    ]
    if symbol is not None:
        conditions.append(DecisionRow.symbol == symbol.upper())
    result = await session.execute(
        select(DecisionRow.symbol, RiskEvaluationRow.position_size_units, RiskEvaluationRow.price)
        .join(RiskEvaluationRow, RiskEvaluationRow.decision_id == DecisionRow.id)
        .where(*conditions)
        .order_by(DecisionRow.decision_at)
    )
    snapshots: list[PaperSnapshot] = []
    for row in result.all():
        symbol_val: str = str(row.symbol)
        units = float(row.position_size_units or 0.0)
        price = float(row.price or 0.0)
        snapshots.append(
            PaperSnapshot(
                symbol=symbol_val,
                notional=units * price,
                basket=frozenset({symbol_val[:3], symbol_val[3:]}),
            )
        )
    return snapshots
