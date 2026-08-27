"""Provider interfaces for news and economic-calendar data (Phase 4).

Agents and persistence depend on these interfaces only; vendor-specific
adapters (Finnhub, synthetic, ...) are constructed behind them and swap
without touching callers (implementation requirement #4). External I/O is the
adapters' job — neither the worker's normalization nor the agents ever speak
to an external service directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.data.content_types import NormalizedEconomicEvent, NormalizedNewsItem


class ContentProviderError(Exception):
    """Base error for news/calendar provider failures."""


class ContentProviderAuthError(ContentProviderError):
    """Authentication/authorization failure (bad/absent/expired credential)."""


class ContentProviderRateLimitError(ContentProviderError):
    """Provider rate limit exceeded (retry later)."""


class ContentProviderTransientError(ContentProviderError):
    """Transient failure (timeout, 5xx, network) — retryable."""


@runtime_checkable
class CalendarProvider(Protocol):
    """Vendor-agnostic economic-calendar interface."""

    name: str

    async def fetch_events(
        self,
        *,
        since: datetime,
        until: datetime,
        symbols: Sequence[str] | None = None,
    ) -> list[NormalizedEconomicEvent]:
        """Return normalized events with ``since <= timestamp_utc < until``."""
        ...

    async def aclose(self) -> None:
        """Release underlying resources."""
        ...


@runtime_checkable
class NewsProvider(Protocol):
    """Vendor-agnostic news interface."""

    name: str

    async def fetch_news(
        self,
        *,
        since: datetime,
        until: datetime,
        symbols: Sequence[str] | None = None,
    ) -> list[NormalizedNewsItem]:
        """Return normalized stories published within the window."""
        ...

    async def aclose(self) -> None:
        """Release underlying resources."""
        ...
