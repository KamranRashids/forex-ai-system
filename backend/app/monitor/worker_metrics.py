"""Per-process worker Prometheus metrics (Phase 12 O-1).

Each worker process serves its own tiny metrics HTTP endpoint so Prometheus can
scrape liveness/cycle/error signals straight from the process that owns them.
The API-side ``/metrics`` can only recompute heartbeat gauges from Redis (the
workers are separate processes); these process-local exporters are the source
of truth for whether a worker's loop is actually alive and making progress.

Metric names are namespaced ``forex_worker_*`` so they never collide with the
API-side ``worker_up`` / ``worker_heartbeat_age_seconds`` gauges. Behaviour is
observation only — nothing here touches any order path (SAFE MODE preserved).
"""

from __future__ import annotations

import os
import time
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, start_http_server

#: Default per-role exporter ports. The compose stack pins these explicitly
#: (WORKER_METRICS_PORT env) so they are visible in one place; the env override
#: lets an operator move any exporter without editing the stack config.
DEFAULT_WORKER_METRICS_PORTS: dict[str, int] = {
    "ingest": 9101,
    "agents": 9102,
    "orchestrator": 9103,
    "alerts": 9104,
    "content": 9105,
}

WORKER_METRICS_PORT_ENV: str = "WORKER_METRICS_PORT"

_STARTED: dict[str, WorkerMetrics] = {}


def port_for_worker(worker: str) -> int:
    """Resolve the exporter port for *worker*, honouring the env override."""
    raw = os.environ.get(WORKER_METRICS_PORT_ENV, "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if 0 < parsed < 65536:
            return parsed
    return DEFAULT_WORKER_METRICS_PORTS[worker]


class WorkerMetrics:
    """Process-local Prometheus metrics for a single worker role."""

    def __init__(self, worker: str, registry: CollectorRegistry) -> None:
        self._worker = worker
        self._registry = registry
        self._up: Any = Gauge(
            "forex_worker_up",
            "1 while this worker's loop is alive, 0 otherwise",
            ("worker",),
            registry=registry,
        ).labels(worker=worker)
        self._heartbeat_ts: Any = Gauge(
            "forex_worker_heartbeat_timestamp_seconds",
            "Unix time (seconds) of this worker's most recent heartbeat touch",
            ("worker",),
            registry=registry,
        ).labels(worker=worker)
        self._cycles: Any = Counter(
            "forex_worker_cycles_total",
            "Loop cycles completed by this worker",
            ("worker",),
            registry=registry,
        ).labels(worker=worker)
        self._errors: Any = Counter(
            "forex_worker_errors_total",
            "Failures recorded by this worker's loop",
            ("worker",),
            registry=registry,
        ).labels(worker=worker)

    @property
    def registry(self) -> CollectorRegistry:
        return self._registry

    def mark_heartbeat(self) -> None:
        """Flag the loop as alive now (call right after ``heartbeat.touch()``)."""
        self._up.set(1)
        self._heartbeat_ts.set(time.time())

    def cycle(self, *, errors: int = 0) -> None:
        """Record one completed loop cycle and any tolerated per-cycle failures."""
        self._cycles.inc()
        if errors > 0:
            self._errors.inc(errors)

    def error(self) -> None:
        """Record an exception that escaped the loop body."""
        self._errors.inc()


def build_worker_metrics(worker: str, registry: CollectorRegistry) -> WorkerMetrics:
    """Build a :class:`WorkerMetrics` bound to *registry* (used by tests)."""
    return WorkerMetrics(worker, registry)


def start_worker_metrics(worker: str) -> WorkerMetrics:
    """Start the worker's metrics HTTP exporter once per process."""
    existing = _STARTED.get(worker)
    if existing is not None:
        return existing
    registry = CollectorRegistry()
    metrics = build_worker_metrics(worker, registry)
    start_http_server(port_for_worker(worker), addr="0.0.0.0", registry=registry)
    _STARTED[worker] = metrics
    return metrics
