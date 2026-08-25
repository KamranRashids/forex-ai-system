"""Canonical Redis topic/channel names (IMPLEMENTATION_PLAN §10)."""

from __future__ import annotations

SCHEMA_VERSION: int = 1


def bars_closed_topic(timeframe: str) -> str:
    """Stream topic consumed by agents (Phase 3+) for a given timeframe."""
    return f"bars.closed.{timeframe}"


PRICES_LIVE_CHANNEL: str = "prices.live"
EVENTS_ALERTS_CHANNEL: str = "events.alerts"

STREAM_MAXLEN_APPROX: int = 100_000


def ingest_lock_key(provider: str) -> str:
    """Advisory lock so only one ingest worker runs per provider."""
    return f"lock:ingest:{provider}"


def latest_price_key(symbol: str) -> str:
    return f"prices.latest:{symbol}"
