"""Worker process entrypoint; role selected via WORKER_ROLE.

Roles:
- ``ingest``      Phase 2 — market data pipeline (this phase)
- ``agents`` | ``orchestrator`` | ``executor`` — later phases; refuse to start
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from datetime import timedelta
from typing import Any

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_redis_client, get_sessionmaker


def _logger() -> Any:
    return structlog.stdlib.get_logger("worker")


async def run_ingest_worker() -> None:
    from app.bus.publisher import RedisEventPublisher
    from app.data.ingest import IngestService, seed_instruments
    from app.data.market_config import get_market_config
    from app.data.providers.factory import build_provider
    from app.monitor.staleness import StalenessMonitor

    log = _logger()
    settings = get_settings()
    session_factory = get_sessionmaker()
    redis = get_redis_client()
    provider = build_provider(settings)
    publisher = RedisEventPublisher(redis, producer_name="ingest")

    service = IngestService(
        settings=settings,
        session_factory=session_factory,
        provider=provider,
        publisher=publisher,
    )
    await service.load_breaker_snapshot()

    async with session_factory() as session:
        seeded = await seed_instruments(session, settings.market_symbols)
        await session.commit()
    instruments_by_symbol = seeded
    timeframes = settings.market_timeframes

    monitor = StalenessMonitor(session_factory=session_factory, publisher=publisher)

    log.warning(
        "SAFE MODE ACTIVE: paper trading only. Live order execution is not implemented anywhere.",
        worker="ingest",
        provider=provider.name,
        symbols=len(seeded),
        timeframes=timeframes,
    )

    interval = timedelta(seconds=settings.ingest_interval_seconds)
    staleness_every = max(1, settings.staleness_poll_seconds // settings.ingest_interval_seconds)

    iteration = 0
    while True:
        # Runtime-tunable universe (admin API overrides) — reread every cycle.
        async with session_factory() as session:
            symbols, timeframes = await get_market_config(session, settings)
        known_symbols = set(instruments_by_symbol)
        new_symbols = [s for s in symbols if s not in known_symbols]
        if new_symbols:
            async with session_factory() as session:
                newly_seeded = await seed_instruments(session, new_symbols)
                await session.commit()
            instruments_by_symbol.update(newly_seeded)
        instruments = [instruments_by_symbol[s] for s in symbols if s in instruments_by_symbol]

        cycle_start = service.now
        result = await service.run_cycle(instruments, timeframes)
        log.info(
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
                log.warning("staleness_check_breaches", count=breached)

        await _drain_backfills(service, redis, session_factory, instruments_by_symbol)

        elapsed = (service.now - cycle_start).total_seconds()
        sleep_for = max(0.5, interval.total_seconds() - elapsed)
        await asyncio.sleep(sleep_for)


async def _drain_backfills(
    service: Any,
    redis: Any,
    session_factory: Any,
    instruments_by_symbol: dict[str, Any],
) -> None:
    """Execute queued admin backfill jobs against the running provider."""
    from datetime import datetime as _dt

    from app.api.v1.admin import drain_backfill_queue
    from app.data.ingest import seed_instruments

    jobs = await drain_backfill_queue(redis)
    for job in jobs:
        try:
            symbols = [s.upper() for s in job.get("symbols", [])]
            unknown = [s for s in symbols if s not in instruments_by_symbol]
            if unknown:
                async with session_factory() as session:
                    seeded = await seed_instruments(session, unknown)
                    await session.commit()
                instruments_by_symbol.update(seeded)

            backfill_instruments = [
                instruments_by_symbol[s] for s in symbols if s in instruments_by_symbol
            ]
            result = await service.run_backfill(
                backfill_instruments,
                job.get("timeframes", []),
                start=_dt.fromisoformat(job["start"]),
                end=_dt.fromisoformat(job["end"]),
            )
            structlog.stdlib.get_logger("worker").info(
                "backfill_job_done",
                symbols=symbols,
                timeframes=job.get("timeframes"),
                inserted=result.inserted,
                gaps=sum(r.gaps_detected for r in result.results),
            )
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker
            structlog.stdlib.get_logger("worker").error("backfill_job_failed", error=str(exc))


async def run_unimplemented_role(role: str) -> None:
    log = _logger()
    log.error(
        "worker_role_not_available_yet",
        role=role,
        arrives_in="Phase 3+ (see IMPLEMENTATION_PLAN.md section 14)",
    )


def main() -> None:
    settings = get_settings()
    configure_logging(log_level=settings.log_level, json_logs=settings.json_logs)
    role = os.getenv("WORKER_ROLE", "ingest").strip().lower()

    runner: dict[str, Any] = {
        "ingest": run_ingest_worker,
        "agents": lambda: run_unimplemented_role("agents"),
        "orchestrator": lambda: run_unimplemented_role("orchestrator"),
        "executor": lambda: run_unimplemented_role("executor"),
    }
    handler = runner.get(role)
    if handler is None:
        _logger().error("unknown_worker_role", role=role)
        raise SystemExit(2)
    with suppress(KeyboardInterrupt):  # pragma: no cover - operator interrupt
        asyncio.run(handler())


if __name__ == "__main__":
    main()
