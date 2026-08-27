"""Risk state + tunable-params API (Phase 5).

Exposes the persisted rolling risk counters (daily-loss / drawdown / exposure)
and the resolved, admins-overridable risk parameters. Read surfaces are
viewer+; mutation of parameters is admin-only and audited.

SAFE MODE: these knobs gate *paper analysis intents* only. None can enable live
order execution.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request

from app.api.deps import AdminUser, DBSession, SettingsDep, client_ip
from app.core.constants import AuditActions
from app.data.decision_repository import load_risk_state
from app.data.risk_config import load_risk_params, set_risk_params
from app.models.audit_log import AuditLog
from app.models.risk_state import RiskStateRow
from app.schemas.decisions import RiskParamsOut, RiskParamsUpdate, RiskStateOut

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/state")
async def risk_state(
    session: DBSession,
    current: AdminUser,  # noqa: ARG001 - RBAC gating for sensitive risk data
) -> dict[str, RiskStateOut | None]:
    """Daily + account aggregate risk counters (admin-only, high sensitivity)."""
    now = datetime.now(UTC)
    daily_key = now.strftime("%Y-%m-%d")
    daily = await load_risk_state(session, scope="daily", period_key=daily_key)
    account = await load_risk_state(session, scope="account", period_key="global")

    def _out(row: RiskStateRow | None) -> RiskStateOut | None:
        if row is None:
            return None
        return RiskStateOut(
            scope=row.scope,
            period_key=row.period_key,
            realized_loss=float(row.realized_loss),
            peak_equity=float(row.peak_equity),
            max_drawdown=float(row.max_drawdown),
            exposure=float(row.exposure),
            updated_at=row.updated_at,
        )

    return {"daily": _out(daily), "account": _out(account)}


@router.get("/params")
async def get_risk_params(
    session: DBSession,
    current: AdminUser,  # noqa: ARG001
    settings: SettingsDep,
) -> RiskParamsOut:
    """Resolved risk parameters (env defaults + runtime overrides)."""
    resolved = await load_risk_params(session, settings)
    return RiskParamsOut(**resolved.to_dict())


@router.put("/params")
async def update_risk_params(
    body: RiskParamsUpdate,
    session: DBSession,
    settings: SettingsDep,
    admin: AdminUser,
    request: Request,
) -> RiskParamsOut:
    """Apply runtime risk overrides (admin-only, audited). Omitted fields stay."""
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    resolved = await set_risk_params(session, actor=admin.email, updates=updates)
    session.add(
        AuditLog(
            actor=admin.email,
            action=AuditActions.RISK_PARAMS_UPDATED,
            entity_type="system_setting",
            entity_id="risk.params",
            after={"updates": updates, "resolved": resolved.to_dict()},
            ip_address=client_ip(request),
        )
    )
    await session.commit()
    return RiskParamsOut(**resolved.to_dict())
