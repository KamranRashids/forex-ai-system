"""Integration: agent pipeline over real PostgreSQL + fakeredis streams.

Flow under test (Phase 3 exit path):
  bar.closed event -> AgentWorker.poll_once -> agents analyze -> rows in
  agent_signals (versioned) -> signal.emitted on signals.stream.
Replay of the same event must not duplicate rows (identity key).
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from app.agents.registry import default_registry
from app.bus.events import Event
from app.data.providers.synthetic import synthetic_candles
from app.data.repository import load_candles, upsert_candles
from app.workers.agent_worker import AgentWorker

pytestmark = [pytest.mark.integration]

PASSWORD = "correct-horse-battery-staple"
SYMBOL = "EURUSD"


async def _seed_series(db_sessionmaker: Any, *, bars: int, end: datetime) -> datetime:
    """Store `bars` synthetic M15 candles ending at the newest closed bucket <= end."""
    from app.data.ingest import seed_instruments
    from app.data.timeframes import previous_closed_bucket

    bucket_end = previous_closed_bucket(end, "M15") + timedelta(minutes=15)
    start = bucket_end - timedelta(minutes=15 * bars)
    candles = synthetic_candles(SYMBOL, "M15", start, bucket_end)

    async with db_sessionmaker() as session:
        await seed_instruments(session, [SYMBOL])
        instrument = await seed_instruments(session, [SYMBOL])
        await upsert_candles(
            session,
            instrument_id=instrument[SYMBOL].id,
            candles=candles,
            source="synthetic",
            timeframe="M15",
        )
        await session.commit()
    return candles[-1].bucket_start


def _worker(db_sessionmaker: Any, fake_redis: Any) -> AgentWorker:
    from app.bus.publisher import RedisEventPublisher

    return AgentWorker(
        session_factory=db_sessionmaker,
        redis=fake_redis,
        publisher=RedisEventPublisher(fake_redis, producer_name="agents"),
        agents=default_registry().all(),
        timeframes=["M15"],
    )


def _bar_event(bucket_ts: datetime) -> dict[str, str]:
    return {
        "symbol": SYMBOL,
        "timeframe": "M15",
        "ts": bucket_ts.isoformat(),
        "open": "1.0850",
        "high": "1.0860",
        "low": "1.0840",
        "close": "1.0855",
        "volume": "1200",
        "source": "synthetic",
    }


async def _publish_bar(fake_redis: Any, bucket_ts: datetime) -> None:
    """Publish a bar.closed envelope exactly as the ingest worker does."""
    payload = _bar_event(bucket_ts)
    event = Event(
        event_type="bar.closed",
        payload=payload,
        producer="ingest",
        produced_at=datetime.now(UTC),
    )
    await fake_redis.xadd("bars.closed.M15", {"data": event.to_json()})


@pytest.mark.asyncio
async def test_bar_event_persists_versioned_signals(db_sessionmaker: Any, fake_redis: Any) -> None:
    last_bucket = await _seed_series(db_sessionmaker, bars=150, end=datetime.now(UTC))
    worker = _worker(db_sessionmaker, fake_redis)
    await worker.ensure_groups()

    # Publish a closed-bar event for the newest stored bucket.
    await _publish_bar(fake_redis, last_bucket)

    result = await worker.poll_once()
    assert result.processed == 1
    assert result.errors == 0
    assert result.signals_written >= 2  # technical + regime persisted

    from app.models.agent_signal import AgentSignalRow
    from sqlalchemy import select

    async with db_sessionmaker() as session:
        rows = (
            (await session.execute(select(AgentSignalRow).where(AgentSignalRow.symbol == SYMBOL)))
            .scalars()
            .all()
        )
    by_agent = {r.agent_id: r for r in rows}
    assert set(by_agent) == {"technical", "regime"}
    tech = by_agent["technical"]
    assert tech.direction in ("LONG", "SHORT", "FLAT")
    assert tech.bucket_ts == last_bucket
    assert tech.agent_version == "1"
    assert tech.run_id  # audit metadata recorded

    # signal.emitted events reached signals.stream
    entries = await fake_redis.xrange("signals.stream")
    emitted_agents: set[str] = set()
    for _entry_id, fields in entries:
        raw = fields.get("data", fields.get(b"data"))
        envelope = json.loads(raw)
        assert envelope["event_type"] == "signal.emitted"
        assert envelope["schema_version"] == 1
        emitted_agents.add(envelope["payload"]["agent_id"])
    assert {"technical", "regime"} <= emitted_agents


@pytest.mark.asyncio
async def test_event_replay_does_not_duplicate_signals(
    db_sessionmaker: Any, fake_redis: Any
) -> None:
    last_bucket = await _seed_series(db_sessionmaker, bars=150, end=datetime.now(UTC))
    worker = _worker(db_sessionmaker, fake_redis)
    await worker.ensure_groups()

    json.dumps(_bar_event(last_bucket))
    await _publish_bar(fake_redis, last_bucket)
    first = await worker.poll_once()
    assert first.processed == 1

    # Replay: same identity key must be a persistence no-op.
    await _publish_bar(fake_redis, last_bucket)
    second = await worker.poll_once()
    assert second.processed == 1
    assert second.signals_written < first.signals_written or second.signals_written == 0

    from app.models.agent_signal import AgentSignalRow
    from sqlalchemy import func, select

    async with db_sessionmaker() as session:
        total = await session.scalar(select(func.count()).select_from(AgentSignalRow))
    assert total == first.signals_written  # no duplicates


@pytest.mark.asyncio
async def test_backpressure_keeps_only_newest_bar_per_pair(
    db_sessionmaker: Any, fake_redis: Any
) -> None:
    await _seed_series(db_sessionmaker, bars=150, end=datetime.now(UTC))
    worker = _worker(db_sessionmaker, fake_redis)
    await worker.ensure_groups()

    old_bucket = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
    new_bucket = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    old = _bar_event(old_bucket)
    new = _bar_event(new_bucket)
    del old, new  # envelopes built inside _publish_bar
    await _publish_bar(fake_redis, old_bucket)
    await _publish_bar(fake_redis, new_bucket)

    result = await worker.poll_once()
    assert result.skipped_stale == 1
    assert result.processed == 1

    # Only the newer bar produced signals: its bucket is the stored one.
    from app.models.agent_signal import AgentSignalRow
    from sqlalchemy import select

    async with db_sessionmaker() as session:
        rows = (
            (await session.execute(select(AgentSignalRow).where(AgentSignalRow.symbol == SYMBOL)))
            .scalars()
            .all()
        )
    assert {r.bucket_ts.replace(tzinfo=UTC) for r in rows} == {new_bucket}


@pytest.mark.asyncio
async def test_signal_api_rbac_and_payload(
    client: httpx.AsyncClient,
    db_sessionmaker: Any,
    fake_redis: Any,
) -> None:
    from tests.integration.conftest import bearer, register_and_login

    last_bucket = await _seed_series(db_sessionmaker, bars=150, end=datetime.now(UTC))
    worker = _worker(db_sessionmaker, fake_redis)
    await worker.ensure_groups()
    await _publish_bar(fake_redis, last_bucket)
    await worker.poll_once()

    tokens = await register_and_login(client, "signal-viewer@example.com", PASSWORD)
    headers = bearer(tokens["access_token"])

    latest = await client.get(
        "/api/v1/signals/latest", params={"symbol": SYMBOL, "timeframe": "M15"}, headers=headers
    )
    assert latest.status_code == 200
    body = latest.json()
    assert {s["agent_id"] for s in body} == {"technical", "regime"}
    tech = next(s for s in body if s["agent_id"] == "technical")
    assert tech["direction"] in ("LONG", "SHORT", "FLAT")
    assert 0.0 <= tech["confidence"] <= 1.0
    assert "votes" in tech["features"]

    history = await client.get(
        "/api/v1/signals",
        params={"symbol": SYMBOL, "timeframe": "M15", "limit": 10},
        headers=headers,
    )
    assert history.status_code == 200
    assert len(history.json()) >= 2

    anon = await client.get("/api/v1/signals/latest", params={"symbol": SYMBOL, "timeframe": "M15"})
    assert anon.status_code == 401


@pytest.mark.asyncio
async def test_agent_latency_sub_second_at_m5_scale(db_sessionmaker: Any, fake_redis: Any) -> None:
    """Exit criterion proxy: per-agent analysis latency well under one second."""
    m5_bars = 150
    end = datetime.now(UTC)
    from app.data.ingest import seed_instruments
    from app.data.timeframes import previous_closed_bucket

    bucket_end = previous_closed_bucket(end, "M5") + timedelta(minutes=5)
    start = bucket_end - timedelta(minutes=5 * m5_bars)
    candles = synthetic_candles("GBPUSD", "M5", start, bucket_end)

    async with db_sessionmaker() as session:
        seeded = await seed_instruments(session, ["GBPUSD"])
        await upsert_candles(
            session,
            instrument_id=seeded["GBPUSD"].id,
            candles=candles,
            source="synthetic",
            timeframe="M5",
        )
        await session.commit()

    worker = _worker(db_sessionmaker, fake_redis)
    fields = _bar_event(candles[-1].bucket_start)
    fields["symbol"] = "GBPUSD"
    fields["timeframe"] = "M5"
    started = time.perf_counter()
    written = await worker.process_bar(fields)
    total_s = time.perf_counter() - started
    assert written >= 2
    assert total_s < 1.0, f"M5 bar processing took {total_s:.3f}s (>1s)"

    async with db_sessionmaker() as session:
        rows = await load_candles(
            session,
            instrument_id=(await seed_instruments(session, ["GBPUSD"]))["GBPUSD"].id,
            timeframe="M5",
            limit=1,
        )
    assert rows, "series vanished?"
