"""Alert persistence + read access (idempotent by ``event_id``, Phase 8).

Alerts arrive on ``alerts.stream`` and are persisted by the ``alerts`` worker.
``save_alert`` is idempotent on the producer-supplied ``event_id``
(``ON CONFLICT DO NOTHING``), which makes at-least-once delivery + restart
catch-up safe: a redelivered stream entry inserts no duplicate row.

Acknowledgment is *observability only*: it flips ``acknowledged_at``/
``acknowledged_by`` and never touches trading, risk, or any control path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert_event import AlertEvent


@dataclass(frozen=True, slots=True)
class AlertIn:
    """Normalized, ready-to-persist alert derived from a bus Event."""

    event_id: str
    event_type: str
    source: str
    severity: str
    title: str
    message: str | None
    symbol: str | None
    timeframe: str | None
    producer: str
    correlation_id: str | None
    occurred_at: datetime
    payload: dict[str, Any]


def _values(alert: AlertIn) -> dict[str, object]:
    return {
        "event_id": alert.event_id,
        "event_type": alert.event_type,
        "source": alert.source,
        "severity": alert.severity,
        "title": alert.title,
        "message": alert.message,
        "symbol": alert.symbol,
        "timeframe": alert.timeframe,
        "producer": alert.producer,
        "correlation_id": alert.correlation_id,
        "occurred_at": alert.occurred_at,
        "payload": alert.payload,
    }


async def save_alert(session: AsyncSession, alert: AlertIn) -> bool:
    """Insert an alert; a replay of the same event_id inserts no duplicate.

    Returns True when a new row was inserted, False when it was a duplicate.
    """
    stmt = (
        pg_insert(AlertEvent)
        .values(_values(alert))
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(AlertEvent.id)
    )
    result = await session.scalar(stmt)
    return result is not None


async def ack_alert(
    session: AsyncSession, event_id: str, *, acknowledged_by: str, at: datetime
) -> bool:
    """Acknowledge an alert (observability only). Returns True if a row was updated."""
    result = await session.execute(
        update(AlertEvent)
        .where(AlertEvent.event_id == event_id, AlertEvent.acknowledged_at.is_(None))
        .values(acknowledged_at=at, acknowledged_by=acknowledged_by)
    )
    cursor = cast("Any", result)
    return (cursor.rowcount or 0) > 0


async def list_alerts(
    session: AsyncSession,
    *,
    source: str | None = None,
    event_type: str | None = None,
    acknowledged: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AlertEvent]:
    """Newest-first alert rows with optional filters."""
    conditions = []
    if source:
        conditions.append(AlertEvent.source == source)
    if event_type:
        conditions.append(AlertEvent.event_type == event_type)
    if acknowledged is True:
        conditions.append(AlertEvent.acknowledged_at.isnot(None))
    elif acknowledged is False:
        conditions.append(AlertEvent.acknowledged_at.is_(None))
    result = await session.execute(
        select(AlertEvent)
        .where(*conditions)
        .order_by(AlertEvent.occurred_at.desc(), AlertEvent.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_pending(session: AsyncSession) -> int:
    """Number of persisted but unacknowledged alerts (used for a gauge)."""
    result = await session.execute(
        select(func.count(AlertEvent.id)).where(AlertEvent.acknowledged_at.is_(None))
    )
    return int(result.scalar_one())


async def get_alert(session: AsyncSession, event_id: str) -> AlertEvent | None:
    """Fetch one alert by its stable event_id, or None."""
    result = await session.execute(
        select(AlertEvent).where(AlertEvent.event_id == event_id).limit(1)
    )
    return result.scalars().first()
