"""Unit tests: worker heartbeat helper (Phase 7 monitor component)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.bus.topics import worker_heartbeat_key
from app.monitor.heartbeat import (
    WORKER_ROLES,
    WorkerHeartbeat,
    heartbeat_status,
    heartbeat_ttl_for_loop,
    read_worker_health,
)
from fakeredis.aioredis import FakeRedis


@pytest.mark.unit
async def test_touch_writes_hash_and_refreshes_ttl() -> None:
    redis = FakeRedis(decode_responses=True)
    hb = WorkerHeartbeat(redis, "ingest", ttl_seconds=60, pid=12345)
    now = datetime(2026, 8, 29, 10, 0, 0, tzinfo=UTC)
    await hb.touch(now=now)
    raw = await redis.hgetall(worker_heartbeat_key("ingest"))
    assert raw["last_seen"] == now.isoformat()
    assert raw["pid"] == "12345"
    ttl = await redis.ttl(worker_heartbeat_key("ingest"))
    assert 1 <= ttl <= 60


@pytest.mark.unit
async def test_clear_removes_key() -> None:
    redis = FakeRedis(decode_responses=True)
    hb = WorkerHeartbeat(redis, "agents", ttl_seconds=60)
    await hb.touch()
    assert await redis.exists(worker_heartbeat_key("agents")) == 1
    await hb.clear()
    assert await redis.exists(worker_heartbeat_key("agents")) == 0


@pytest.mark.unit
async def test_read_worker_health_classifies_up_stale_down() -> None:
    redis = FakeRedis(decode_responses=True)
    now = datetime(2026, 8, 29, 10, 0, 0, tzinfo=UTC)
    # "ingest" fresh, "agents" stale (older than TTL), "content"/"orchestrator" absent.
    fresh = WorkerHeartbeat(redis, "ingest", ttl_seconds=60)
    await fresh.touch(now=now - timedelta(seconds=5))
    stale = WorkerHeartbeat(redis, "agents", ttl_seconds=60)
    await stale.touch(now=now - timedelta(seconds=120))

    health = {
        h.role: h
        for h in await read_worker_health(redis, roles=WORKER_ROLES, now=now, ttl_seconds=60)
    }
    assert health["ingest"].status == "up"
    assert health["ingest"].age_seconds == pytest.approx(5.0)
    assert health["agents"].status == "stale"
    assert health["content"].status == "down"
    assert health["orchestrator"].status == "down"
    assert health["content"].age_seconds is None


@pytest.mark.unit
async def test_read_worker_health_custom_roles() -> None:
    redis = FakeRedis(decode_responses=True)
    hb = WorkerHeartbeat(redis, "custom", ttl_seconds=10)
    await hb.touch()
    health = await read_worker_health(redis, roles=("custom",), ttl_seconds=10)
    assert health[0].role == "custom"
    assert health[0].status == "up"


@pytest.mark.unit
def test_heartbeat_status_classifier() -> None:
    assert heartbeat_status(None, 60) == "down"
    assert heartbeat_status(0.0, 60) == "up"
    assert heartbeat_status(60, 60) == "up"  # boundary inclusive
    assert heartbeat_status(60.1, 60) == "stale"


@pytest.mark.unit
def test_heartbeat_ttl_for_loop_exceeds_cadence() -> None:
    # A content worker looping every 300s must not appear down between cycles.
    assert heartbeat_ttl_for_loop(300, min_ttl_seconds=60) == 900
    # Fast loops fall to the floor.
    assert heartbeat_ttl_for_loop(1, min_ttl_seconds=60) == 60
    assert heartbeat_ttl_for_loop(0, min_ttl_seconds=60) == 60
    # min_ttl floor dominates small loops.
    assert heartbeat_ttl_for_loop(5, min_ttl_seconds=120) == 120


@pytest.mark.unit
async def test_declared_ttl_used_for_classification() -> None:
    """A worker that declares a longer TTL is 'up' even when older than the
    caller's fallback default (e.g. slow-loop content worker between cycles)."""
    redis = FakeRedis(decode_responses=True)
    now = datetime(2026, 8, 29, 10, 0, 0, tzinfo=UTC)
    # Declared TTL 900s, heartbeat 300s old -> up (not stale with a 60s default).
    await WorkerHeartbeat(redis, "content", ttl_seconds=900).touch(now=now - timedelta(seconds=300))
    health = await read_worker_health(redis, roles=("content",), now=now, ttl_seconds=60)
    assert health[0].status == "up"
    assert health[0].ttl_seconds == 900


@pytest.mark.unit
def test_worker_roles_expected() -> None:
    assert set(WORKER_ROLES) == {"ingest", "agents", "content", "orchestrator", "alerts"}
