"""Agent worker runtime entrypoint (Phase 3)."""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.shutdown import ShutdownCoordinator
from app.db.session import get_redis_client, get_sessionmaker

logger = structlog.stdlib.get_logger(__name__)


async def run_agents_worker(settings: Settings | None = None) -> None:
    """Consume closed-bar streams and run the agent registry until cancelled."""
    from app.agents.registry import default_registry
    from app.bus.publisher import RedisEventPublisher
    from app.monitor.heartbeat import (
        WorkerHeartbeat,
        heartbeat_ttl_for_loop,
    )
    from app.workers.agent_worker import AgentWorker

    resolved = settings or get_settings()
    session_factory: async_sessionmaker[AsyncSession] = get_sessionmaker()
    redis = get_redis_client()
    publisher = RedisEventPublisher(redis, producer_name="agents")

    registry = default_registry()
    worker = AgentWorker(
        session_factory=session_factory,
        redis=redis,
        publisher=publisher,
        agents=registry.all(),
        timeframes=resolved.market_timeframes,
    )
    await worker.ensure_groups()

    heartbeat = WorkerHeartbeat(
        redis,
        "agents",
        ttl_seconds=heartbeat_ttl_for_loop(5, min_ttl_seconds=resolved.heartbeat_ttl_seconds),
    )
    shutdown = ShutdownCoordinator()

    logger.warning(
        "SAFE MODE ACTIVE: paper trading only. Live order execution is not implemented anywhere.",
        worker="agents",
        agents=[a.id for a in registry.all()],
        timeframes=resolved.market_timeframes,
    )

    try:
        while not shutdown.should_stop:
            await heartbeat.touch()
            batch = await worker.poll_once()
            if batch.processed or batch.skipped_stale or batch.errors:
                logger.info(
                    "agent_batch",
                    processed=batch.processed,
                    skipped_stale=batch.skipped_stale,
                    written=batch.signals_written,
                    errors=batch.errors,
                )
            await _sleep()
    finally:
        await heartbeat.clear()
        shutdown.close()


async def _sleep() -> None:  # pragma: no cover - thin alias for testability
    import asyncio

    await asyncio.sleep(0.5)
