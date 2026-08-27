"""Decision + risk response models (Phase 5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Any
    run_id: str
    symbol: str
    timeframe: str
    bucket_ts: datetime
    fused_direction: str
    confidence: float
    agreement: float
    status: str
    veto_code: str | None = None
    veto_reason: str | None = None
    inputs_hash: str
    weights: dict[str, Any]
    code_versions: dict[str, Any]
    rationale: str | None = None
    decision_at: datetime
    valid_until: datetime | None = None


class RiskGateOut(BaseModel):
    code: str
    ok: bool
    detail: str


class RiskEvaluationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Any
    symbol: str
    timeframe: str
    bucket_ts: datetime
    position_size_units: float | None = None
    price: float | None = None
    atr: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    rr_ratio: float | None = None
    risk_pct_account: float | None = None
    exposure_ok: bool
    correlation_ok: bool
    daily_loss_ok: bool
    drawdown_ok: bool
    passed: bool
    reasons: list[RiskGateOut] | list[dict[str, Any]]
    evaluated_at: datetime


class RiskStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    scope: str
    period_key: str
    realized_loss: float
    peak_equity: float
    max_drawdown: float
    exposure: float
    updated_at: datetime


class RiskParamsOut(BaseModel):
    max_risk_pct_account: float
    max_exposure_pct: float
    max_daily_loss_pct: float
    max_drawdown_pct: float
    min_rr: float
    sl_atr_multiple: float
    tp_atr_multiple: float
    vol_target_pct: float
    correlation_cap_pct: float
    risk_enabled: bool
    paper_equity: float


class RiskParamsUpdate(BaseModel):
    max_risk_pct_account: float | None = None
    max_exposure_pct: float | None = None
    max_daily_loss_pct: float | None = None
    max_drawdown_pct: float | None = None
    min_rr: float | None = None
    sl_atr_multiple: float | None = None
    tp_atr_multiple: float | None = None
    vol_target_pct: float | None = None
    correlation_cap_pct: float | None = None
