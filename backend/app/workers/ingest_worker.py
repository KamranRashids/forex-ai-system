"""Ingest worker runtime (moved from worker_main in Phase 3 refactor)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bus.publisher import RedisEventPublisher
from app.core.config import Settings, get_settings
from app.core.shutdown import ShutdownCoordinator
from app.data.ingest import IngestService
from app.db.session import get_redis_client, get_sessionmaker

logger = structlog.stdlib.get_logger(__name__)


async def run_ingest_worker(settings: Settings | None = None) -> None:
    """Drive the market-data ingestion loop until cancelled."""
    from app.data.market_config import get_market_config
    from app.data.providers.factory import build_provider
    from app.monitor.heartbeat import (
        WorkerHeartbeat,
        heartbeat_ttl_for_loop,
    )
    from app.monitor.staleness import StalenessMonitor

    resolved = settings or get_settings()
    session_factory: async_sessionmaker[AsyncSession] = get_sessionmaker()
    redis = get_redis_client()
    provider = build_provider(resolved)
    publisher = RedisEventPublisher(redis, producer_name="ingest")

    service = IngestService(
        settings=resolved,
        session_factory=session_factory,
        provider=provider,
        publisher=publisher,
    )
    await service.load_breaker_snapshot()

    async with session_factory() as session:
        seeded = await seed_instruments(session, resolved.market_symbols)
        await session.commit()
    instruments_by_symbol = dict(seeded)

    monitor = StalenessMonitor(session_factory=session_factory, publisher=publisher)
    heartbeat = WorkerHeartbeat(
        redis,
        "ingest",
        ttl_seconds=heartbeat_ttl_for_loop(
            resolved.ingest_interval_seconds, min_ttl_seconds=resolved.heartbeat_ttl_seconds
        ),
    )
    shutdown = ShutdownCoordinator()

    logger.warning(
        "SAFE MODE ACTIVE: paper trading only. Live order execution is not implemented anywhere.",
        worker="ingest",
        provider=provider.name,
        symbols=len(instruments_by_symbol),
        timeframes=resolved.market_timeframes,
    )

    interval = timedelta(seconds=resolved.ingest_interval_seconds)
    staleness_every = max(1, resolved.staleness_poll_seconds // resolved.ingest_interval_seconds)
    iteration = 0

    try:
        while not shutdown.should_stop:
            await heartbeat.touch()
            # Runtime-tunable universe (admin overrides) reread every cycle.
            async with session_factory() as session:
                symbols, timeframes = await get_market_config(session, resolved)
            new_symbols = [s for s in symbols if s not in instruments_by_symbol]
            if new_symbols:
                async with session_factory() as session:
                    newly = await seed_instruments(session, new_symbols)
                    await session.commit()
                instruments_by_symbol.update(newly)
            instruments = [instruments_by_symbol[s] for s in symbols if s in instruments_by_symbol]

            cycle_start = service.now
            result = await service.run_cycle(instruments, timeframes)
            logger.info(
                "ingest_cycle",
                inserted=result.inserted,
                updated=sum(r.updated for r in result.results),
                gaps=sum(r.gaps_detected for r in result.results),
                up_to_date=len(result.up_to_date),
                breaker_skipped=len(result.skipped_breaker),
                failed=len(result.failed),
                first_failure=(result.failed[0].skipped_reason if result.failed else None),
            )
            await service.persist_breaker_snapshot()

            iteration += 1
            if iteration % staleness_every == 0:
                findings = await monitor.check(instruments, timeframes, now=service.now)
                breached = sum(1 for f in findings if f.breached)
                if breached:
                    logger.warning("staleness_check_breaches", count=breached)
                await _publish_staleness_latest(redis, findings, breached)

            await drain_backfills(service, redis, session_factory, instruments_by_symbol)

            elapsed = (service.now - cycle_start).total_seconds()
            await asyncio_sleep(max(0.5, interval.total_seconds() - elapsed))
    finally:
        await heartbeat.clear()
        shutdown.close()
        await provider.aclose()


async def seed_instruments(session: Any, symbols: list[str]) -> dict[str, Any]:
    from app.data.ingest import seed_instruments as _seed

    return await _seed(session, symbols)


async def drain_backfills(
    service: Any, redis: Any, session_factory: Any, instruments_by_symbol: dict[str, Any]
) -> None:
    """Execute queued admin backfill jobs against the running provider."""
    from datetime import datetime as _dt

    from app.api.v1.admin import drain_backfill_queue

    jobs = await drain_backfill_queue(redis)
    for job in jobs:
        try:
            symbols = [s.upper() for s in job.get("symbols", [])]
            unknown = [s for s in symbols if s not in instruments_by_symbol]
            if unknown:
                async with session_factory() as session:
                    newly = await seed_instruments(session, unknown)
                    await session.commit()
                instruments_by_symbol.update(newly)

            backfill_instruments = [
                instruments_by_symbol[s] for s in symbols if s in instruments_by_symbol
            ]
            result = await service.run_backfill(
                backfill_instruments,
                job.get("timeframes", []),
                start=_dt.fromisoformat(job["start"]),
                end=_dt.fromisoformat(job["end"]),
            )
            logger.info(
                "backfill_job_done",
                symbols=symbols,
                inserted=result.inserted,
                gaps=sum(r.gaps_detected for r in result.results),
            )
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker
            logger.error("backfill_job_failed", error=str(exc))


async def asyncio_sleep(seconds: float) -> None:  # pragma: no cover - thin alias
    import asyncio

    await asyncio.sleep(seconds)


async def _publish_staleness_latest(redis: Any, findings: list[Any], breached: int) -> None:
    """Write the current staleness picture to Redis for API Prometheus gauges."""
    from app.bus.topics import STALENESS_LATEST_KEY

    payload = {
        "breached": breached,
        "checked": len(findings),
        "max_age_seconds": max(
            (f.age_seconds for f in findings if f.age_seconds is not None),
            default=0,
        ),
        "timeframes": sorted({f.timeframe for f in findings}),
    }
    try:
        await redis.set(STALENESS_LATEST_KEY, json.dumps(payload))
    except Exception:  # noqa: BLE001 - monitoring must never break ingestion
        logger.debug("staleness_latest_write_failed")
