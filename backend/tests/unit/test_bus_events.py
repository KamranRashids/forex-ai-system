"""Unit tests: event envelope + topic naming (pure logic)."""

from __future__ import annotations

from datetime import UTC, datetime

import app.monitor  # noqa: F401 - ensures monitor package imports cleanly
import pytest
from app.bus.events import Event
from app.bus.topics import (
    EVENTS_ALERTS_CHANNEL,
    PRICES_LIVE_CHANNEL,
    bars_closed_topic,
    ingest_lock_key,
    latest_price_key,
)


def _event() -> Event:
    return Event(
        event_type="bar.closed",
        payload={"symbol": "EURUSD", "close": 1.0855},
        producer="ingest",
        produced_at=datetime(2026, 8, 21, 10, 15, tzinfo=UTC),
    )


@pytest.mark.unit
def test_event_json_roundtrip_preserves_all_fields() -> None:
    event = _event()
    parsed = Event.from_json(event.to_json())
    assert parsed == event
    assert parsed.schema_version == 1


@pytest.mark.unit
def test_event_from_bytes_and_defaults() -> None:
    raw = _event().to_json().encode()
    parsed = Event.from_json(raw)
    assert parsed.producer == "ingest"
    # Missing optional correlation_id defaults to empty string.
    minimal = Event.from_json(
        '{"event_type":"x","payload":{},"producer":"p","produced_at":"2026-08-21T10:00:00+00:00"}'
    )
    assert minimal.correlation_id == ""


@pytest.mark.unit
def test_event_rejects_malformed_timestamp() -> None:
    import pytest as _pytest

    with _pytest.raises(Exception):  # noqa: B017 - ValueError via datetime.fromisoformat
        Event.from_json('{"event_type":"x","payload":{},"producer":"p","produced_at":"not-a-date"}')


@pytest.mark.unit
def test_topic_names_follow_plan_topology() -> None:
    assert bars_closed_topic("M15") == "bars.closed.M15"
    assert bars_closed_topic("D1") == "bars.closed.D1"
    assert PRICES_LIVE_CHANNEL == "prices.live"
    assert EVENTS_ALERTS_CHANNEL == "events.alerts"
    assert ingest_lock_key("oanda") == "lock:ingest:oanda"
    assert latest_price_key("EURUSD") == "prices.latest:EURUSD"
