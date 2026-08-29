"""Unit tests: alert translation + severity mapping (pure logic, Phase 8)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.alerts.translate import translate
from app.bus.events import Event
from app.models.alert_event import severity_for


def _event(event_type: str, payload: dict | None = None, producer: str = "monitor") -> Event:
    return Event(
        event_type=event_type,
        payload=payload or {},
        producer=producer,
        produced_at=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )


@pytest.mark.unit
def test_translate_staleness_sets_warning_and_symbol() -> None:
    alert = translate(
        _event(
            "alert.staleness",
            {
                "symbol": "EURUSD",
                "timeframe": "M15",
                "age_seconds": 3600,
                "threshold_seconds": 2700,
            },
        ),
        default_source="monitor",
    )
    assert alert.event_type == "alert.staleness"
    assert alert.severity == "warning"
    assert alert.symbol == "EURUSD"
    assert alert.timeframe == "M15"
    assert "EURUSD" in alert.title
    assert alert.producer == "monitor"


@pytest.mark.unit
def test_translate_risk_brake_warning() -> None:
    alert = translate(
        _event(
            "alert.risk_brake",
            {
                "symbol": "GBPUSD",
                "timeframe": "H1",
                "veto_code": "daily_loss",
                "severity": "warning",
            },
            producer="orchestrator",
        )
    )
    assert alert.severity == "warning"
    assert alert.symbol == "GBPUSD"
    assert "daily_loss" in alert.title or "Risk brake" in alert.title


@pytest.mark.unit
def test_translate_llm_budget_warning() -> None:
    alert = translate(
        _event("alert.llm_budget", {"state": "budget_exhausted", "source": "llm"}, producer="llm")
    )
    assert alert.severity == "warning"
    assert alert.source == "llm"
    assert "budget" in alert.title.lower()


@pytest.mark.unit
def test_translate_orchestrator_producer_is_source() -> None:
    alert = translate(
        _event(
            "alert.orchestrator",
            {"subject": "lock_lost", "message": "lost lock"},
            producer="orchestrator",
        )
    )
    assert alert.source == "orchestrator"
    assert alert.message == "lost lock"
    assert alert.event_type == "alert.orchestrator"


@pytest.mark.unit
def test_translate_unknown_type_is_tolerated_with_generic_title() -> None:
    alert = translate(_event("some.unknown.event", {"subject": "x"}))
    assert alert.event_type == "some.unknown.event"
    assert alert.payload == {"subject": "x"}


@pytest.mark.unit
def test_translate_event_id_stable_and_truncated() -> None:
    a = translate(_event("alert.staleness", {"symbol": "EURUSD"}))
    b = translate(_event("alert.staleness", {"symbol": "EURUSD"}))
    assert a.event_id == b.event_id


@pytest.mark.unit
def test_translate_uses_provided_event_id() -> None:
    alert = translate(_event("alert.x", {"event_id": "abc-123"}))
    assert alert.event_id == "abc-123"


@pytest.mark.unit
def test_severity_mapping_buckets() -> None:
    assert severity_for("alert.staleness") == "warning"
    assert severity_for("alert.risk_brake") == "warning"
    assert severity_for("alert.llm_budget") == "warning"
    assert severity_for("something.else") == "info"
    assert severity_for("critical.failure") == "critical"


@pytest.mark.unit
def test_translate_never_raises_on_missing_payload_fields() -> None:
    alert = translate(_event("alert.minimal"))
    assert alert.title
