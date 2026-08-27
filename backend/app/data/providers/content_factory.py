"""Content provider selection (news/calendar) with graceful degradation.

Configuration rules (implementation requirements #3 and #6):

- ``synthetic`` (the default) always works with zero keys and is deterministic.
- ``finnhub`` requires ``FINNHUB_API_TOKEN``; when the token is missing/blank, the
  provider gracefully degrades to ``synthetic`` so the system never crashes and
  never leaks credentials. Runtime failures (rate limit, upstream outage) are
  handled by the worker, which falls back to synthetic per cycle — see
  ``app/workers/content_runtime.py``.
"""

from __future__ import annotations

from typing import Any, cast

from app.core.config import Settings
from app.data.providers.content_base import CalendarProvider, NewsProvider


def _build(provider_name: str, settings: Settings, kind: str) -> Any:
    name = (provider_name or "").strip().lower()
    if name == "finnhub":
        token = settings.finnhub_api_token
        if not token:
            # Degrade to synthetic when Finnhub is configured but unkeyed.
            from app.data.providers.synthetic_calendar import SyntheticCalendarProvider
            from app.data.providers.synthetic_news import SyntheticNewsProvider

            if kind == "calendar":
                return SyntheticCalendarProvider()
            return SyntheticNewsProvider()
        from app.data.providers.finnhub_calendar import FinnhubCalendarProvider
        from app.data.providers.finnhub_news import FinnhubNewsProvider

        if kind == "calendar":
            return FinnhubCalendarProvider.from_settings(settings)
        return FinnhubNewsProvider.from_settings(settings)
    # Default: synthetic.
    from app.data.providers.synthetic_calendar import SyntheticCalendarProvider
    from app.data.providers.synthetic_news import SyntheticNewsProvider

    if kind == "calendar":
        return SyntheticCalendarProvider()
    return SyntheticNewsProvider()


def build_calendar_provider(settings: Settings) -> CalendarProvider:
    return cast(
        CalendarProvider,
        _build(settings.calendar_provider, settings, "calendar"),
    )


def build_news_provider(settings: Settings) -> NewsProvider:
    return cast(NewsProvider, _build(settings.news_provider, settings, "news"))
