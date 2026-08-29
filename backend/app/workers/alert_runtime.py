"""Alerts worker runtime entrypoint (Phase 8, decision #3)."""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.shutdown import ShutdownCoordinator
from app.db.session import get_redis_client, get_sessionmaker
from app.monitor.heartbeat import WorkerHeartbeat, heartbeat_ttl_for_loop
from app.workers.alert_worker import ALERT_POLL_SECONDS, AlertWorker

logger = structlog.stdlib.get_logger(__name__)


async def run_alerts_worker(settings: Settings | None = None) -> None:
    """Drive the alerts consumer until cancelled.

    At-least-once delivery + idempotent persistence (see :class:`AlertWorker`);
    on restart, un-acked stream entries are replayed and collapsed into single
    rows by ``event_id``.
    """
    resolved = settings or get_settings()
    session_factory: async_sessionmaker[AsyncSession] = get_sessionmaker()
    redis = get_redis_client()
    worker = AlertWorker(session_factory=session_factory, redis=redis, settings=resolved)

    await worker.ensure_group()

    heartbeat = WorkerHeartbeat(
        redis,
        "alerts",
        ttl_seconds=heartbeat_ttl_for_loop(
            ALERT_POLL_SECONDS, min_ttl_seconds=resolved.heartbeat_ttl_seconds
        ),
    )
    shutdown = ShutdownCoordinator()

    logger.warning(
        "SAFE MODE ACTIVE: paper trading only. Live order execution is not implemented anywhere.",
        worker="alerts",
    )

    try:
        while not shutdown.should_stop:
            await heartbeat.touch()
            try:
                batch = await worker.poll_once()
                if batch.processed or batch.replayed or batch.errors:
                    logger.info(
                        "alerts_batch",
                        processed=batch.processed,
                        replayed=batch.replayed,
                        errors=batch.errors,
                        inserted=batch.inserted,
                    )
            except Exception as exc:  # noqa: BLE001 - survive transient bus failures
                logger.exception("alerts_cycle_failed", error=str(exc))
            await asyncio.sleep(ALERT_POLL_SECONDS)
    finally:
        await heartbeat.clear()
        shutdown.close()
