"""Decisions + risk read API (Phase 5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DBSession
from app.data.decision_repository import (
    load_decisions,
    load_latest_decision,
    load_risk_evaluations,
)
from app.models.decision import DecisionRow
from app.models.risk_evaluation import RiskEvaluationRow
from app.schemas.decisions import DecisionOut, RiskEvaluationOut, RiskGateOut

router = APIRouter(prefix="/decisions", tags=["decisions"])

_TIMEFRAME_PATTERN = "^(M5|M15|H1|H4|D1)$"


def _decision_out(row: DecisionRow) -> DecisionOut:
    return DecisionOut(
        id=row.id,
        run_id=row.run_id,
        symbol=row.symbol,
        timeframe=row.timeframe,
        bucket_ts=row.bucket_ts,
        fused_direction=row.fused_direction,
        confidence=float(row.confidence),
        agreement=float(row.agreement),
        status=row.status,
        veto_code=row.veto_code,
        veto_reason=row.veto_reason,
        inputs_hash=row.inputs_hash,
        weights=row.weights or {},
        code_versions=row.code_versions or {},
        rationale=row.rationale,
        decision_at=row.decision_at,
        valid_until=row.valid_until,
    )


def _risk_out(row: RiskEvaluationRow) -> RiskEvaluationOut:
    reasons_raw: list[Any] = row.reasons or []
    reasons: list[RiskGateOut] = []
    for item in reasons_raw:
        if isinstance(item, dict):
            reasons.append(
                RiskGateOut(
                    code=str(item.get("code", "")),
                    ok=bool(item.get("ok", False)),
                    detail=str(item.get("detail", "")),
                )
            )
    return RiskEvaluationOut(
        id=row.id,
        symbol=row.symbol,
        timeframe=row.timeframe,
        bucket_ts=row.bucket_ts,
        position_size_units=(
            float(row.position_size_units) if row.position_size_units is not None else None
        ),
        price=float(row.price) if row.price is not None else None,
        atr=float(row.atr) if row.atr is not None else None,
        stop_loss=float(row.stop_loss) if row.stop_loss is not None else None,
        take_profit=float(row.take_profit) if row.take_profit is not None else None,
        rr_ratio=float(row.rr_ratio) if row.rr_ratio is not None else None,
        risk_pct_account=float(row.risk_pct_account) if row.risk_pct_account is not None else None,
        exposure_ok=row.exposure_ok,
        correlation_ok=row.correlation_ok,
        daily_loss_ok=row.daily_loss_ok,
        drawdown_ok=row.drawdown_ok,
        passed=row.passed,
        reasons=reasons,
        evaluated_at=row.evaluated_at,
    )


@router.get("/latest")
async def latest_decision(
    session: DBSession,
    current: CurrentUser,
    symbol: Annotated[str, Query(min_length=6, max_length=12)],
    timeframe: Annotated[str, Query(pattern=_TIMEFRAME_PATTERN)],
    fresh_only: bool = True,
) -> DecisionOut | None:
    """Newest decision for a pair/timeframe (viewer+)."""
    row = await load_latest_decision(
        session,
        symbol=symbol,
        timeframe=timeframe.upper(),
        now=datetime.now(UTC) if fresh_only else None,
    )
    return _decision_out(row) if row is not None else None


@router.get("")
async def decision_history(
    session: DBSession,
    current: CurrentUser,
    symbol: Annotated[str, Query(min_length=6, max_length=12)],
    timeframe: Annotated[str, Query(pattern=_TIMEFRAME_PATTERN)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    order: Literal["asc", "desc"] = "desc",
) -> list[DecisionOut]:
    """Recent decision history for a pair/timeframe (viewer+)."""
    rows = await load_decisions(
        session, symbol=symbol, timeframe=timeframe.upper(), limit=limit, order=order
    )
    return [_decision_out(r) for r in rows]


@router.get("/risk-evaluations")
async def risk_evaluations(
    session: DBSession,
    current: CurrentUser,
    symbol: Annotated[str, Query(min_length=6, max_length=12)],
    timeframe: Annotated[str, Query(pattern=_TIMEFRAME_PATTERN)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RiskEvaluationOut]:
    """Risk evaluation snapshots (sizing + gate outcomes) for a pair (viewer+)."""
    rows = await load_risk_evaluations(
        session, symbol=symbol, timeframe=timeframe.upper(), limit=limit
    )
    return [_risk_out(r) for r in rows]
