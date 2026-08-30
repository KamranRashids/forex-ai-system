"""Unit tests: AlertWorker consumer-group control flow (Phase 9).

These exercise the worker's async control flow (group creation, XREADGROUP
outage handling, poison tolerance, ACK-after-commit, duplicate/replay counting)
against fakeredis with ``save_alert`` patched so no PostgreSQL is required.
The Postgres-backed idempotent persistence itself is covered by the integration
suite (``tests/integration/test_alerts_pipeline.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from app.bus.events import Event
from app.bus.topics import ALERTS_GROUP, ALERTS_STREAM
from fakeredis.aioredis import FakeRedis


def _event() -> Event:
    return Event(
        event_type="alert.staleness",
        payload={"symbol": "EURUSD", "timeframe": "M15", "source": "monitor"},
        producer="monitor",
        produced_at=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )


def _poison() -> dict[str, str]:
    return {"data": "this is not a valid event envelope"}


class _FakeSession:
    """Async-session stand-in with commit/rollback and a flag for rollback."""

    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeSessionFactory:
    """Returns one fake session per ``poll_once`` call."""

    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []

    def __call__(self) -> _FakeSession:
        session = _FakeSession()
        self.sessions.append(session)
        return session


async def _make_worker(
    monkeypatch: Any, *, save_alert_result: bool = True
) -> tuple[Any, FakeRedis, _FakeSessionFactory]:
    from app.workers.alert_worker import AlertWorker

    redis = FakeRedis(decode_responses=True)
    factory = _FakeSessionFactory()

    async def _fake_save_alert(session: Any, alert: Any) -> bool:
        return save_alert_result

    monkeypatch.setattr("app.workers.alert_worker.save_alert", _fake_save_alert)
    worker = AlertWorker(session_factory=factory, redis=redis)
    await worker.ensure_group()
    return worker, redis, factory


@pytest.mark.asyncio
async def test_ensure_group_is_idempotent(monkeypatch: Any) -> None:
    from app.workers.alert_worker import AlertWorker

    redis = FakeRedis(decode_responses=True)
    worker = AlertWorker(session_factory=_FakeSessionFactory(), redis=redis)

    await worker.ensure_group()
    # A second call must tolerate BUSYGROUP (group already exists).
    await worker.ensure_group()

    groups = await redis.xinfo_groups(ALERTS_STREAM)
    assert any(g["name"] == ALERTS_GROUP for g in groups)
    await redis.aclose()


@pytest.mark.asyncio
async def test_poll_once_survives_bus_outage(monkeypatch: Any) -> None:
    worker, redis, _factory = await _make_worker(monkeypatch)

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("redis gone")

    monkeypatch.setattr(redis, "xreadgroup", _boom)
    result = await worker.poll_once()

    assert result.processed == 0
    assert result.errors == 0
    assert result.replayed == 0  # graceful: returns an empty batch, no crash
    await redis.aclose()


@pytest.mark.asyncio
async def test_poison_entry_is_replayed_and_valid_events_still_process(monkeypatch: Any) -> None:
    worker, redis, factory = await _make_worker(monkeypatch)
    await redis.xadd(ALERTS_STREAM, _poison())
    await redis.xadd(ALERTS_STREAM, {"data": _event().to_json()})

    result = await worker.poll_once()

    # Poison is skipped (replayed) but never kills the loop; valid event processed.
    assert result.processed == 1
    assert result.replayed == 1
    assert result.errors == 0
    await redis.aclose()


@pytest.mark.asyncio
async def test_duplicate_redelivery_is_replayed_not_processed(monkeypatch: Any) -> None:
    worker, redis, _factory = await _make_worker(monkeypatch, save_alert_result=False)
    await redis.xadd(ALERTS_STREAM, {"data": _event().to_json()})

    result = await worker.poll_once()

    # save_alert returned False (duplicate event_id) → not a new insert.
    assert result.processed == 0
    assert result.replayed == 1
    assert result.errors == 0
    await redis.aclose()


@pytest.mark.asyncio
async def test_persist_failure_is_counted_and_does_not_crash_loop(monkeypatch: Any) -> None:
    worker, redis, factory = await _make_worker(monkeypatch)

    async def _raise_on_save(session: Any, alert: Any) -> bool:
        raise RuntimeError("db write failed")

    monkeypatch.setattr("app.workers.alert_worker.save_alert", _raise_on_save)
    await redis.xadd(ALERTS_STREAM, {"data": _event().to_json()})

    result = await worker.poll_once()

    assert result.errors == 1
    assert result.replayed == 0
    assert result.processed == 0
    # The session is still committed (worker ACKs after commit, never crashes).
    assert factory.sessions and factory.sessions[0].committed is True
    await redis.aclose()
