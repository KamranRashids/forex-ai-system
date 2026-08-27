"""Normalized news / economic-calendar value types (Phase 4).

These are the *canonical* internal representations produced by provider
adapters and consumed by persistence + agents. Provider-specific quirks live
only inside adapters; agents and the DB layer never see raw Finnhub (or any
other vendor) payloads.

Safety notes:
- ``raw_payload`` may be preserved for audit/debug but is **sanitized** by
  adapters before it reaches here (a provider API token is never stored).
- All timestamps are timezone-aware UTC. Missing optional values are ``None``
  (e.g. an upcoming calendar event has no ``actual`` yet).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SAFE_MODE_NOTE = "SAFE MODE: analysis/reference data only; no order path."


#: Canonical importance levels for economic events.
IMPORTANCE_LEVELS: frozenset[str] = frozenset({"low", "medium", "high"})


def _ensure_utc(ts: datetime, *, what: str) -> datetime:
    if ts.tzinfo is None:
        raise ValueError(f"{what} must be timezone-aware UTC; got naive {ts!r}")
    return ts.astimezone()


@dataclass(frozen=True, slots=True)
class NormalizedEconomicEvent:
    """One normalized economic-calendar event.

    ``external_id`` is the provider's stable identifier where available
    (else ``None``); persistence combines ``provider`` + ``external_id`` with
    stable hashed fields for idempotency so two genuinely different records
    never collide (implementation requirement #2).
    """

    provider: str
    title: str
    timestamp_utc: datetime
    importance: str
    currency: str = ""
    symbols: tuple[str, ...] = ()
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    surprise_score: float | None = None
    external_id: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timestamp_utc",
            _ensure_utc(self.timestamp_utc, what="timestamp_utc"),
        )
        if self.importance not in IMPORTANCE_LEVELS:
            raise ValueError(
                f"importance must be one of {sorted(IMPORTANCE_LEVELS)}; got {self.importance!r}"
            )


@dataclass(frozen=True, slots=True)
class NormalizedNewsItem:
    """One normalized news/story.

    ``external_id`` is the provider's stable article id where available (else
    ``None``); persistence derives a deterministic dedup key from stable,
    provider-independent fields (headline + url + published time), never from
    ingestion time.
    """

    provider: str
    headline: str
    published_utc: datetime
    url: str | None = None
    summary: str | None = None
    symbols: tuple[str, ...] = ()
    external_id: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "published_utc",
            _ensure_utc(self.published_utc, what="published_utc"),
        )
