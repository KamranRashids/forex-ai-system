"""Canonical Redis topic/channel names (IMPLEMENTATION_PLAN §10)."""

from __future__ import annotations

SCHEMA_VERSION: int = 1


def bars_closed_topic(timeframe: str) -> str:
    """Stream topic consumed by agents (Phase 3+) for a given timeframe."""
    return f"bars.closed.{timeframe}"


PRICES_LIVE_CHANNEL: str = "prices.live"
EVENTS_ALERTS_CHANNEL: str = "events.alerts"
SIGNALS_STREAM: str = "signals.stream"
#: Durable orchestrator decision output stream (Phase 5).
DECISIONS_STREAM: str = "decisions.stream"

STREAM_MAXLEN_APPROX: int = 100_000


def ingest_lock_key(provider: str) -> str:
    """Advisory lock so only one ingest worker runs per provider."""
    return f"lock:ingest:{provider}"


def orchestrator_lock_key() -> str:
    """Advisory lock so only one orchestrator worker runs."""
    return "lock:orchestrator"


def latest_price_key(symbol: str) -> str:
    return f"prices.latest:{symbol}"


def worker_heartbeat_key(role: str) -> str:
    """Redis key holding a worker's live heartbeat hash (Phase 7)."""
    return f"worker:heartbeat:{role}"


#: Redis key whose value is the latest staleness findings (JSON) written by the
#: ingest worker each cycle so the API can expose Prometheus gauges.
STALENESS_LATEST_KEY: str = "monitor:staleness:latest"
#: Redis counter of alert events published (used for the Prometheus gauge).
ALERTS_TOTAL_KEY: str = "monitor:alerts:total"
