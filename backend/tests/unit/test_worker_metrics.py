"""Unit tests: worker process-local Prometheus metrics (Phase 12 O-1)."""

from __future__ import annotations

import pytest
from app.monitor.worker_metrics import (
    DEFAULT_WORKER_METRICS_PORTS,
    WORKER_METRICS_PORT_ENV,
    WorkerMetrics,
    build_worker_metrics,
    port_for_worker,
)
from prometheus_client import CollectorRegistry, generate_latest


def _text(metrics: WorkerMetrics) -> str:
    return generate_latest(metrics.registry).decode()


def test_default_ports_cover_every_worker_role() -> None:
    assert set(DEFAULT_WORKER_METRICS_PORTS) == {
        "ingest",
        "agents",
        "orchestrator",
        "alerts",
        "content",
    }
    # Distinct ports, so every exporter can bind simultaneously.
    assert len(set(DEFAULT_WORKER_METRICS_PORTS.values())) == 5


def test_port_for_worker_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WORKER_METRICS_PORT_ENV, raising=False)
    assert port_for_worker("ingest") == DEFAULT_WORKER_METRICS_PORTS["ingest"]


def test_port_for_worker_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WORKER_METRICS_PORT_ENV, "9500")
    assert port_for_worker("ingest") == 9500


def test_port_for_worker_ignores_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WORKER_METRICS_PORT_ENV, "not-a-port")
    assert port_for_worker("ingest") == DEFAULT_WORKER_METRICS_PORTS["ingest"]


def test_metrics_registered_with_worker_label() -> None:
    metrics = build_worker_metrics("ingest", CollectorRegistry())
    text = _text(metrics)
    assert 'forex_worker_up{worker="ingest"}' in text
    assert 'forex_worker_heartbeat_timestamp_seconds{worker="ingest"}' in text
    assert 'forex_worker_cycles_total{worker="ingest"}' in text
    assert 'forex_worker_errors_total{worker="ingest"}' in text


def test_mark_heartbeat_sets_liveness_gauges() -> None:
    metrics = build_worker_metrics("orchestrator", CollectorRegistry())
    metrics.mark_heartbeat()
    text = _text(metrics)
    assert 'forex_worker_up{worker="orchestrator"} 1.0' in text
    assert "forex_worker_heartbeat_timestamp_seconds" in text


def test_cycle_increments_and_tallies_errors() -> None:
    metrics = build_worker_metrics("content", CollectorRegistry())
    metrics.cycle()
    metrics.cycle(errors=0)
    metrics.cycle(errors=2)
    text = _text(metrics)
    assert 'forex_worker_cycles_total{worker="content"} 3.0' in text
    assert 'forex_worker_errors_total{worker="content"} 2.0' in text


def test_error_records_single_failure() -> None:
    metrics = build_worker_metrics("alerts", CollectorRegistry())
    metrics.error()
    metrics.error()
    assert 'forex_worker_errors_total{worker="alerts"} 2.0' in _text(metrics)


def test_metrics_with_fresh_registry_never_collide() -> None:
    a = build_worker_metrics("ingest", CollectorRegistry())
    b = build_worker_metrics("agents", CollectorRegistry())
    assert "ingest" in _text(a) and "ingest" not in _text(b)
    assert "agents" in _text(b) and "agents" not in _text(a)
