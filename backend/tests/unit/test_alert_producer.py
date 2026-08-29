"""Unit tests: alert producers write to alerts.stream; worker decode (Phase 8)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.bus.events import Event
from app.bus.publisher import RedisEventPublisher
from app.bus.topics import ALERTS_STREAM
from app.workers.alert_worker import _decode_event
from fakeredis.aioredis import FakeRedis


def _event() -> Event:
    return Event(
        event_type="alert.staleness",
        payload={"symbol": "EURUSD", "source": "monitor"},
        producer="monitor",
        produced_at=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_publish_alert_writes_to_durable_stream() -> None:
    redis = FakeRedis(decode_responses=True)
    pub = RedisEventPublisher(redis, producer_name="ingest")
    await pub.publish_alert(_event())

    entries = await redis.xrange(ALERTS_STREAM)
    assert len(entries) == 1
    _, fields = entries[0]
    decoded = _decode_event(fields)
    assert decoded is not None
    assert decoded.event_type == "alert.staleness"
    assert decoded.payload["symbol"] == "EURUSD"
    await redis.aclose()


@pytest.mark.unit
def test_decode_event_returns_none_for_poison() -> None:
    assert _decode_event({}) is None
    assert _decode_event({"data": "not-json"}) is None
    assert _decode_event({"data": b"also-not-json"}) is None


@pytest.mark.unit
def test_decode_event_roundtrip() -> None:
    fields = {"data": _event().to_json()}
    decoded = _decode_event(fields)
    assert decoded == _event()
