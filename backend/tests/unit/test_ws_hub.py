"""Unit tests: WebSocket hub framing + stream reading (Phase 8, decision #7)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.bus.events import Event
from app.bus.topics import ALERTS_STREAM, DECISIONS_STREAM, SIGNALS_STREAM
from app.ws.hub import (
    ALLOWED_TOPICS,
    current_stream_id,
    decode_stream_entries,
    parse_subscribe,
    read_once,
    topic_for_stream,
)
from fakeredis.aioredis import FakeRedis


def _event(event_type: str = "alert.staleness") -> Event:
    return Event(
        event_type=event_type,
        payload={"symbol": "EURUSD"},
        producer="monitor",
        produced_at=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )


@pytest.mark.unit
def test_allowed_topics_include_three_subscribable() -> None:
    assert frozenset({"alerts", "signals", "decisions"}) == ALLOWED_TOPICS
    assert "fills" not in ALLOWED_TOPICS


@pytest.mark.unit
def test_parse_subscribe_valid() -> None:
    msg = parse_subscribe('{"type":"subscribe","topics":["alerts","signals"]}')
    assert isinstance(msg, dict)
    assert msg["type"] == "subscribe"
    assert msg["topics"] == ["alerts", "signals"]


@pytest.mark.unit
def test_parse_subscribe_rejects_bad_input() -> None:
    assert isinstance(parse_subscribe("not json"), str)
    assert isinstance(parse_subscribe('{"type":"other"}'), str)
    assert isinstance(parse_subscribe('{"type":"subscribe","topics":"alerts"}'), str)
    assert isinstance(parse_subscribe('{"type":"subscribe","topics":[1,2]}'), str)
    assert isinstance(parse_subscribe("x" * 5000), str)


@pytest.mark.unit
def test_topic_for_stream_mapping() -> None:
    assert topic_for_stream(ALERTS_STREAM) == "alerts"
    assert topic_for_stream(SIGNALS_STREAM) == "signals"
    assert topic_for_stream(DECISIONS_STREAM) == "decisions"
    assert topic_for_stream("not.a.stream") is None


@pytest.mark.unit
def test_decode_stream_entries_handles_poison() -> None:
    good = _event().to_json()
    entries = [("1-0", {"data": good}), ("2-0", {"data": "not-json"}), ("3-0", {})]
    decoded = decode_stream_entries(entries)
    assert decoded[0] == ("1-0", _event())
    assert decoded[1][1] is None
    assert decoded[2][1] is None
    assert decoded[2][0] == "3-0"


@pytest.mark.asyncio
async def test_read_once_advances_cursor_and_tolerates_outage() -> None:
    redis = FakeRedis(decode_responses=True)
    await redis.xadd(ALERTS_STREAM, {"data": _event().to_json()})

    base = await current_stream_id(redis, ALERTS_STREAM)
    assert base and base != "0-0"

    # Reading from "0-0" (a hypothetical pre-baseline cursor) sees the entry.
    entries, since = await read_once(redis, {"alerts": "0-0"}, {"alerts"})
    assert "alerts" in entries
    assert len(entries["alerts"]) == 1
    assert since["alerts"] != "0-0"

    # Baselined to the current head and no new entries => nothing after base.
    tail, _ = await read_once(redis, {"alerts": base}, {"alerts"})
    assert tail.get("alerts", []) == []

    # Advancing past the consumed id again => nothing new.
    again, _ = await read_once(redis, since, {"alerts"})
    assert again.get("alerts", []) == []
    await redis.aclose()

    # Outage (bad client) should not raise.
    broken = FakeRedis(decode_responses=True)
    await broken.aclose()

    class _Down:
        async def xread(self, *a, **k):  # noqa: ANN002, ANN003
            raise OSError("down")

    out, since2 = await read_once(_Down(), {"alerts": "0"}, {"alerts"})
    assert out == {}
    assert since2 == {"alerts": "0"}
