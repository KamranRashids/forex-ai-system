"""Alerts endpoints: list persisted alert events + admin acknowledgment (Phase 8).

Alerts are durable, observability records persisted by the ``alerts`` worker
from ``alerts.stream``. Read access is viewer+ (consistent with other read-only
resources). Acknowledgment is an **admin** action that is strictly observability
only — it never affects trading, risk, or any control path.

SAFE MODE: these endpoints only read and acknowledge alert records.
"""

from __future__ import annotations

from contextlib import suppress

from fastapi import APIRouter, Request

from app.api.deps import AdminUser, CurrentUser, DBSession, client_ip
from app.core.constants import AuditActions
from app.core.errors import InvalidInputError, NotFoundError
from app.core.metrics import ALERTS_PENDING
from app.core.security import utcnow as _utcnow
from app.data.alerts_repository import ack_alert, count_pending, get_alert, list_alerts
from app.models.audit_log import AuditLog
from app.schemas.alerts import AlertListOut, AlertOut

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("")
async def read_alerts(
    session: DBSession,
    _user: CurrentUser,
    source: str | None = None,
    event_type: str | None = None,
    acknowledged: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> AlertListOut:
    """List persisted alerts, newest first (viewer+)."""
    if limit < 1 or limit > 500:
        raise InvalidInputError("limit must be between 1 and 500")
    if offset < 0:
        raise InvalidInputError("offset must be non-negative")

    rows = await list_alerts(
        session,
        source=source,
        event_type=event_type,
        acknowledged=acknowledged,
        limit=limit,
        offset=offset,
    )
    with suppress(Exception):  # noqa: BLE001 - gauge refresh must never fail the request
        ALERTS_PENDING.set(await count_pending(session))
    items = [AlertOut.model_validate(row) for row in rows]
    return AlertListOut(items=items, total=len(items), limit=limit, offset=offset)


@router.post("/{event_id}/ack")
async def acknowledge_alert(
    event_id: str,
    session: DBSession,
    admin: AdminUser,
    request: Request,
) -> AlertOut:
    """Acknowledge an alert (admin, observability only)."""
    await ack_alert(session, event_id, acknowledged_by=admin.email, at=_utcnow())

    row = await get_alert(session, event_id)
    if row is None:
        raise NotFoundError(f"alert {event_id!r} not found")

    session.add(
        AuditLog(
            actor=admin.email,
            action=AuditActions.ALERT_ACKNOWLEDGED,
            entity_type="alert_event",
            entity_id=event_id,
            after={
                "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None
            },
            ip_address=client_ip(request),
        )
    )
    return AlertOut.model_validate(row)
