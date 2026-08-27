"""Orchestrator worker runtime entrypoint (Phase 5)."""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import get_redis_client, get_sessionmaker
from app.workers.orchestrator_worker import SCAN_EVERY_CYCLES, OrchestratorWorker

logger = structlog.stdlib.get_logger(__name__)


async def run_orchestrator(settings: Settings | None = None) -> None:
    """Drive the orchestrator until cancelled (single active instance)."""
    from app.bus.publisher import RedisEventPublisher

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

    if not await worker.acquire_lock():
        logger.error(
            "orchestrator_lock_busy",
            detail="another orchestrator already holds the lock; refusing to start",
        )
        return
    await worker.ensure_groups()

    logger.warning(
        "SAFE MODE ACTIVE: paper trading only. Live order execution is not implemented anywhere.",
        worker="orchestrator",
        poll_ms=resolved.orch_poll_ms,
    )

    cycle: int = 0
    while True:
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
