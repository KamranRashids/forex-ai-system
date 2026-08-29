"""Translate bus Events into normalized :class:`AlertIn` records (Phase 8).

Each producer emits an Event on ``alerts.stream`` via ``publish_alert``; the
``alerts`` worker decodes it and calls :func:`translate` to build a title,
severity, and entity context for persistence + WebSocket fan-out.

The translator is deliberately tolerant: unknown event types still persist
(generic title) so the at-least-once stream never drops a source event. Titles
are template strings formatted from the payload — payload values are
string-coerced and truncated to keep titles bounded.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from app.bus.events import Event
from app.data.alerts_repository import AlertIn
from app.models.alert_event import severity_for

_TITLE_MAX: int = 255


def _s(payload: dict[str, Any], key: str, default: str = "") -> str:
    val = payload.get(key)
    if val is None:
        return default
    return str(val)


def _truncate(value: str, length: int = _TITLE_MAX) -> str:
    return value if len(value) <= length else value[: length - 1] + "…"


def digest_event_id(event: Event) -> str:
    """Canonical, collision-safe identity for an alert event.

    Prefers a caller-supplied ``event_id`` in the payload; otherwise derives a
    hash from the event type + producer + timestamp so duplicate redeliveries
    of the same underlying occurrence map to one row.

    This is the **single source of truth** for the durable (persisted) alert
    identity. It is used both when persisting ``alert_events.event_id`` and when
    enriching the live WebSocket frame (see ``app/api/v1/realtime.py``), so the
    live ``event_id`` always equals the REST ``AlertOut.event_id``.
    """
    provided = _s(event.payload, "event_id")
    if provided:
        return _truncate(provided, 64)
    material = "|".join(
        [
            event.event_type,
            event.producer,
            event.produced_at.isoformat(),
            _s(event.payload, "symbol"),
            _s(event.payload, "timeframe"),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


_TITLES: dict[str, str] = {
    "alert.staleness": "Stale market data: {symbol} {timeframe}",
    "alert.risk_brake": "Risk brake tripped: {action}",
    "alert.llm_budget": "LLM daily budget guard: {state}",
    "alert.orchestrator": "Orchestrator/system alert: {subject}",
    "alert.system": "System alert: {subject}",
}


def translate(event: Event, *, default_source: str = "monitor") -> AlertIn:
    """Build an :class:`AlertIn` from a bus Event (must never raise)."""
    payload = event.payload or {}
    symbol = payload.get("symbol") or payload.get("pair") or None
    timeframe = payload.get("timeframe") or None

    subject = _s(payload, "subject") or _s(payload, "reason") or event.event_type.split(".")[-1]
    template = _TITLES.get(event.event_type, "Alert: {subject}")
    try:
        title = template.format(
            symbol=symbol or "n/a",
            timeframe=timeframe or "n/a",
            action=subject,
            state=subject,
            subject=subject,
        )
    except (KeyError, IndexError, ValueError):
        title = f"Alert: {event.event_type}"

    message = _s(payload, "message") or None

    return AlertIn(
        event_id=digest_event_id(event),
        event_type=event.event_type,
        source=_s(payload, "source") or event.producer or default_source,
        severity=_s(payload, "severity") or severity_for(event.event_type),
        title=_truncate(title),
        message=message,
        symbol=str(symbol)[:12] if symbol else None,
        timeframe=str(timeframe)[:8] if timeframe else None,
        producer=event.producer,
        correlation_id=event.correlation_id or None,
        occurred_at=event.produced_at or datetime.now(UTC),
        payload=payload,
    )
