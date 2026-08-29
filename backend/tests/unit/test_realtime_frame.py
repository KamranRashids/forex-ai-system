"""Unit tests: WebSocket event frame identity (Phase 8).

The live WS ``event`` frame must carry the same canonical durable ``event_id``
that the alerts worker persists, so the frontend can key live events identically
to the REST ``AlertOut.event_id`` and never collapse distinct alerts.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.alerts.translate import digest_event_id, translate
from app.api.v1.realtime import _event_frame
from app.bus.events import Event


def _event(event_type: str, payload: dict, producer: str = "monitor") -> Event:
    return Event(
        event_type=event_type,
        payload=payload,
        producer=producer,
        produced_at=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )


@pytest.mark.unit
def test_event_frame_carries_canonical_event_id() -> None:
    event = _event("alert.staleness", {"symbol": "EURUSD", "timeframe": "M15"})
    frame = _event_frame("alerts", event)
    assert frame["type"] == "event"
    assert frame["topic"] == "alerts"
    assert frame["data"]["event_id"] == digest_event_id(event)
    # The live identity must equal the durable persisted identity.
    assert frame["data"]["event_id"] == translate(event).event_id


@pytest.mark.unit
def test_event_frame_event_id_matches_persisted_for_derived_digest() -> None:
    # Distinct events that share event_type + occurred_at + symbol must NOT be
    # collapsed: the digest includes the producer, so identities differ.
    a = _event("alert.risk_brake", {"symbol": "GBPUSD"}, producer="orchestrator")
    b = _event("alert.risk_brake", {"symbol": "GBPUSD"}, producer="monitor")
    fa = _event_frame("alerts", a)
    fb = _event_frame("alerts", b)
    assert fa["data"]["event_id"] != fb["data"]["event_id"]
    assert fa["data"]["event_id"] == digest_event_id(a)
    assert fb["data"]["event_id"] == digest_event_id(b)


@pytest.mark.unit
def test_event_frame_uses_provided_event_id() -> None:
    event = _event("alert.x", {"event_id": "abc-123"})
    frame = _event_frame("alerts", event)
    assert frame["data"]["event_id"] == "abc-123"
    assert frame["data"]["event_id"] == translate(event).event_id
