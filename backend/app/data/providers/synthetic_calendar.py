"""Deterministic synthetic economic-calendar provider (default; zero-key)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.data.content_types import NormalizedEconomicEvent
from app.data.providers.synthetic_content import synthetic_calendar_events


class SyntheticCalendarProvider:
    """Adapts the deterministic generator to the CalendarProvider interface."""

    name = "synthetic"

    async def fetch_events(
        self,
        *,
        since: datetime,
        until: datetime,
        symbols: Sequence[str] | None = None,
    ) -> list[NormalizedEconomicEvent]:
        return synthetic_calendar_events(since, until)

    async def aclose(self) -> None:
        return None
