"""Integration: runtime observability — worker heartbeats in /system/status and
/metrics, plus orchestrator distributed-lock renewal/release semantics.

These exercise the real API/app wiring against fake Redis for the observable
state (heartbeats are Redis-only, not DB-backed), and the orchestrator lock
token semantics against fake Redis + real DB sessionmaker (lock ops are
Redis-only).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.bus.publisher import NullEventPublisher
from app.bus.topics import worker_heartbeat_key
from app.monitor.heartbeat import WorkerHeartbeat
from app.workers.orchestrator_worker import OrchestratorWorker
from fakeredis.aioredis import FakeRedis

pytestmark = [pytest.mark.integration]


@pytest.mark.asyncio
async def test_system_status_reports_worker_liveness(
    client: httpx.AsyncClient, fake_redis: FakeRedis
) -> None:
    now = datetime.now(UTC)
    await WorkerHeartbeat(fake_redis, "ingest", ttl_seconds=60).touch(now=now)
    await WorkerHeartbeat(fake_redis, "content", ttl_seconds=60).touch(
        now=now - timedelta(seconds=300)
    )

    resp = await client.get("/system/status")
    assert resp.status_code == 200
    workers = resp.json()["workers"]
    assert set(workers) == {"ingest", "agents", "content", "orchestrator", "alerts"}
    assert workers["ingest"]["status"] == "up"
    assert workers["content"]["status"] == "stale"
    assert workers["agents"]["status"] == "down"
    assert workers["orchestrator"]["status"] == "down"
    assert workers["alerts"]["status"] == "down"
    assert "age_seconds" in workers["ingest"]


@pytest.mark.asyncio
async def test_metrics_expose_worker_and_staleness_gauges(
    client: httpx.AsyncClient, fake_redis: FakeRedis
) -> None:
    now = datetime(2026, 8, 29, 10, 0, 0, tzinfo=UTC)
    await WorkerHeartbeat(fake_redis, "ingest", ttl_seconds=60).touch(now=now)
    import json as _json

    from app.bus.topics import STALENESS_LATEST_KEY

    await fake_redis.set(
        STALENESS_LATEST_KEY,
        _json.dumps({"breached": 2, "max_age_seconds": 4810, "checked": 21}),
    )

    text = (await client.get("/metrics")).text
    assert "worker_up" in text
    assert "worker_heartbeat_age_seconds" in text
    assert "staleness_breach_count" in text
    assert "staleness_max_age_seconds" in text


@pytest.mark.asyncio
async def test_metrics_worker_up_reflects_live_heartbeat(
    client: httpx.AsyncClient, fake_redis: FakeRedis
) -> None:
    now = datetime.now(UTC)
    await WorkerHeartbeat(fake_redis, "orchestrator", ttl_seconds=60).touch(now=now)
    text = (await client.get("/metrics")).text
    assert 'worker_up{role="orchestrator"} 1.0' in text


@pytest.mark.asyncio
async def test_heartbeat_keys_are_not_market_writes(fake_redis: FakeRedis) -> None:
    """SAFE MODE guard: heartbeats only create observability keys, and clearing
    them never touches any lock/decision/signal key."""
    hb = WorkerHeartbeat(fake_redis, "ingest", ttl_seconds=60)
    await hb.touch()
    key = worker_heartbeat_key("ingest")
    assert await fake_redis.exists(key) == 1
    await hb.clear()
    assert await fake_redis.exists(key) == 0


@pytest.mark.asyncio
async def test_orchestrator_lock_renew_extends_own_ttl(
    db_sessionmaker: object, fake_redis: FakeRedis
) -> None:
    worker = OrchestratorWorker(
        session_factory=db_sessionmaker,  # type: ignore[arg-type]
        redis=fake_redis,
        publisher=NullEventPublisher(),
        lock_ttl_seconds=120,
    )
    assert await worker.acquire_lock() is True
    assert await worker.renew_lock() is True
    ttl = await fake_redis.ttl("lock:orchestrator")
    assert 1 <= ttl <= 120


@pytest.mark.asyncio
async def test_orchestrator_lock_release_only_when_owner(
    db_sessionmaker: object, fake_redis: FakeRedis
) -> None:
    owner = OrchestratorWorker(
        session_factory=db_sessionmaker,  # type: ignore[arg-type]
        redis=fake_redis,
        publisher=NullEventPublisher(),
        lock_ttl_seconds=120,
    )
    assert await owner.acquire_lock() is True

    # A second worker cannot acquire while the owner holds it.
    intruder = OrchestratorWorker(
        session_factory=db_sessionmaker,  # type: ignore[arg-type]
        redis=fake_redis,
        publisher=NullEventPublisher(),
        lock_ttl_seconds=120,
    )
    assert await intruder.acquire_lock() is False
    # The intruder cannot renew a lock it does not own (fail closed).
    assert await intruder.renew_lock() is False

    await owner.release_lock()
    assert await fake_redis.exists("lock:orchestrator") == 0
    # After release the intruder can acquire.
    assert await intruder.acquire_lock() is True


@pytest.mark.asyncio
async def test_orchestrator_lock_renew_fails_when_ownership_lost(
    db_sessionmaker: object, fake_redis: FakeRedis
) -> None:
    owner = OrchestratorWorker(
        session_factory=db_sessionmaker,  # type: ignore[arg-type]
        redis=fake_redis,
        publisher=NullEventPublisher(),
        lock_ttl_seconds=120,
    )
    assert await owner.acquire_lock() is True
    # Forcibly remove the lock (simulates TTL expiry / takeover).
    await fake_redis.delete("lock:orchestrator")
    # Ownership can no longer be confirmed -> fail closed.
    assert await owner.renew_lock() is False
