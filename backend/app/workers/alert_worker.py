"""Alerts worker: persist alerts.stream into alert_events (Phase 8, decision #3).

Consumes ``alerts.stream`` via the ``alerts`` consumer group (at-least-once,
like the orchestrator). Each entry is translated to a normalized alert and
persisted idempotently (``event_id`` unique key), then acknowledged. On restart
the group's pending/``0`` offset replays un-acked entries for catch-up —
idempotent persistence makes redelivery safe.

SAFE MODE: this worker only reads a stream and writes observable alert rows.
It cannot place orders, mutate trading/risk state, or affect any control path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.alerts.translate import translate
from app.bus.events import Event
from app.bus.topics import ALERTS_GROUP, ALERTS_STREAM
from app.core.config import Settings, get_settings
from app.core.metrics import ALERT_EVENTS_TOTAL
from app.data.alerts_repository import save_alert

logger = structlog.stdlib.get_logger(__name__)

#: Poll cadence of the alert worker (seconds).
ALERT_POLL_SECONDS: int = 1
CONSUMER_NAME: str = "alerts-1"


@dataclass(slots=True)
class AlertBatchResult:
    processed: int = 0
    replayed: int = 0
    errors: int = 0
    inserted: dict[str, int] = field(default_factory=dict)


class AlertWorker:
    """Consumes alerts.stream and persists normalized rows to alert_events."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings | None = None,
        now: datetime | None = None,
    ) -> None:
        self._sessions = session_factory
        self._redis = redis
        self._settings = settings or get_settings()
        self._now = now or datetime.now(UTC)

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(ALERTS_STREAM, ALERTS_GROUP, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001 - BUSYGROUP == already exists
            if "BUSYGROUP" not in str(exc):
                raise

    async def poll_once(self, *, count: int = 100) -> AlertBatchResult:
        """Read + persist one batch of alert events from the consumer group."""
        result = AlertBatchResult()
        streams: dict[str, str] = {ALERTS_STREAM: ">"}
        try:
            response = await self._redis.xreadgroup(
                ALERTS_GROUP,
                CONSUMER_NAME,
                streams,  # type: ignore[arg-type]
                count=count,
            )
        except Exception as exc:  # noqa: BLE001 - bus outage surviving the loop
            logger.exception("alerts_poll_failed", error=str(exc))
            return result

        if not response:
            return result

        acks: list[tuple[str, str]] = []
        async with self._sessions() as session:
            for stream_name, entries in response:
                for entry_id, fields in entries:
                    event = _decode_event(fields)
                    if event is None:
                        acks.append((stream_name, entry_id))
                        result.replayed += 1
                        continue
                    try:
                        alert = translate(event, default_source=event.producer or "monitor")
                        inserted = await save_alert(session, alert)
                        src = alert.source or "unknown"
                        result.inserted[src] = result.inserted.get(src, 0) + 1
                        ALERT_EVENTS_TOTAL.labels(source=src).inc()
                        if not inserted:
                            result.replayed += 1
                        else:
                            result.processed += 1
                        acks.append((stream_name, entry_id))
                    except Exception as exc:  # noqa: BLE001 - never kill the loop
                        result.errors += 1
                        logger.exception(
                            "alert_persist_failed", error=str(exc), event_id=fields.get("data")
                        )
                        acks.append((stream_name, entry_id))
            await session.commit()

        for _stream_name, entry_id in acks:
            try:
                await self._redis.xack(ALERTS_STREAM, ALERTS_GROUP, entry_id)
            except Exception:  # noqa: BLE001 - ack failure is transient
                logger.debug("alerts_ack_failed", entry_id=entry_id)
        return result


def _decode_event(fields: dict[str, object]) -> Event | None:
    """Decode the envelope from a stream entry, or None for poison entries."""
    raw = fields.get("data")
    if not raw:
        return None
    try:
        return Event.from_json(cast("str | bytes", raw))
    except Exception:  # noqa: BLE001 - malformed envelope: skip poison entry
        return None
