"""Orchestrator worker runtime entrypoint (Phase 5)."""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.shutdown import ShutdownCoordinator
from app.db.session import get_redis_client, get_sessionmaker
from app.workers.orchestrator_worker import SCAN_EVERY_CYCLES, OrchestratorWorker

logger = structlog.stdlib.get_logger(__name__)


async def _emit_orchestrator_alert(publisher: object, subject: str, detail: str) -> None:
    """Best-effort durable ``alert.orchestrator`` sentinel (Phase 8 producer)."""
    from datetime import UTC, datetime

    from app.bus.events import Event

    event = Event(
        event_type="alert.orchestrator",
        payload={
            "source": "orchestrator",
            "severity": "warning",
            "subject": subject,
            "message": detail,
        },
        producer="orchestrator",
        produced_at=datetime.now(UTC),
    )
    try:
        await publisher.publish_alert(event)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - alerting must never crash the worker
        logger.debug("orchestrator_alert_publish_failed", subject=subject)


async def run_orchestrator(settings: Settings | None = None) -> None:
    """Drive the orchestrator until cancelled (single active instance).

    Acquisition is retried for up to one lease TTL so that, after a hard crash
    of a previous owner, a recovered instance can take over as soon as the old
    lease expires — without ever allowing two live owners. On shutdown we fail
    closed: we stop, rather than proceed, whenever ownership cannot be
    confirmed. Exiting only happens on an explicit shutdown signal.
    """
    from app.bus.publisher import RedisEventPublisher
    from app.monitor.heartbeat import WorkerHeartbeat, heartbeat_ttl_for_loop

    resolved = settings or get_settings()
    session_factory: async_sessionmaker[AsyncSession] = get_sessionmaker()
    redis = get_redis_client()
    publisher = RedisEventPublisher(redis, producer_name="orchestrator")

    worker = OrchestratorWorker(
        session_factory=session_factory,
        redis=redis,
        publisher=publisher,
        settings=resolved,
    )

    shutdown = ShutdownCoordinator()
    retry_window = float(worker.lock_ttl)
    started = asyncio.get_event_loop().time()
    acquired = await worker.acquire_lock()
    while not acquired and not shutdown.should_stop:
        remaining = retry_window - (asyncio.get_event_loop().time() - started)
        if remaining <= 0:
            logger.error(
                "orchestrator_lock_busy",
                detail="could not acquire the orchestrator lock within the lease window; "
                "another orchestrator is active; refusing to start",
            )
            shutdown.close()
            return
        logger.warning(
            "orchestrator_lock_pending",
            detail="orchestrator lock already held; waiting to take over after lease expiry",
            wait_seconds=min(remaining, 1.0),
        )
        await shutdown.wait(timeout=min(remaining, 1.0))
        if not shutdown.should_stop:
            acquired = await worker.acquire_lock()
    if shutdown.should_stop:
        shutdown.close()
        return
    await worker.ensure_groups()

    heartbeat = WorkerHeartbeat(
        redis,
        "orchestrator",
        ttl_seconds=heartbeat_ttl_for_loop(
            max(1, int(resolved.orch_poll_ms / 1000)),
            min_ttl_seconds=resolved.heartbeat_ttl_seconds,
        ),
    )

    logger.warning(
        "SAFE MODE ACTIVE: paper trading only. Live order execution is not implemented anywhere.",
        worker="orchestrator",
        poll_ms=resolved.orch_poll_ms,
    )

    cycle: int = 0
    try:
        while not shutdown.should_stop:
            await heartbeat.touch()
            # Renew ownership before doing any work; if we can no longer confirm
            # ownership we fail closed and stop (another orchestrator is active).
            if not await worker.renew_lock():
                logger.error(
                    "orchestrator_lock_lost",
                    detail="could not confirm lock ownership; stopping to avoid dual-active",
                )
                await _emit_orchestrator_alert(
                    publisher, "lock_lost", "could not confirm orchestrator lock ownership"
                )
                break
            try:
                batch = await worker.poll_once()
                if batch.processed or batch.replayed or batch.errors:
                    logger.info(
                        "orchestrator_batch",
                        processed=batch.processed,
                        replayed=batch.replayed,
                        errors=batch.errors,
                        statuses=batch.status_count,
                    )
            except Exception as exc:  # noqa: BLE001 - survive transient bus failures
                logger.exception("orchestrator_poll_failed", error=str(exc))

            cycle += 1
            if cycle % SCAN_EVERY_CYCLES == 0:
                await worker.scan_all()
            await asyncio.sleep(resolved.orch_poll_ms / 1000.0)
    finally:
        # Graceful release only if we still own the lock (token-guarded).
        await worker.release_lock()
        await heartbeat.clear()
        shutdown.close()
