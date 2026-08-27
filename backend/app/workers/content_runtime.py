"""Content poller worker: news + economic-calendar ingestion (Phase 4).

Responsible for all external I/O to news/calendar providers (the *only* place
providers are called). On each cycle it:

- builds the configured providers (gracefully degrading to deterministic
  ``synthetic`` when Finnhub is unkeyed),
- fetches the look-back window from the providers,
- normalizes (adapter output is already normalized) and persists with
  deduplication via :func:`app.data.content_repository`,
- on a runtime provider failure (rate limit, upstream outage, auth) it catches
  the error, logs it, and falls back to synthetic for that cycle so the loop
  never crashes (implementation requirement #3/#6).

SAFE MODE: read-only reference data ingestion. No order path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.data.content_repository import save_events, save_news
from app.data.content_types import NormalizedEconomicEvent, NormalizedNewsItem
from app.data.providers.content_base import ContentProviderError
from app.data.providers.factory import build_calendar, build_news
from app.db.session import get_sessionmaker

logger = structlog.stdlib.get_logger(__name__)


async def run_content_worker(settings: Settings | None = None) -> None:
    """Poll news/calendar providers until cancelled."""

    resolved = settings or get_settings()
    session_factory: async_sessionmaker[AsyncSession] = get_sessionmaker()
    calendar = build_calendar(resolved)
    news = build_news(resolved)

    logger.warning(
        "SAFE MODE ACTIVE: paper trading only. Live order execution is not implemented anywhere.",
        worker="content",
        news_provider=news.name,
        calendar_provider=calendar.name,
    )

    try:
        while True:
            await _cycle(session_factory, resolved, calendar, news)
            await _sleep(min(resolved.news_poll_seconds, resolved.calendar_poll_seconds))
    finally:
        await calendar.aclose()
        await news.aclose()


async def _cycle(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    calendar: object,
    news: object,
) -> None:
    now = datetime.now(UTC)
    since = now - timedelta(hours=settings.news_lookback_hours)
    until = now

    events_inserted = await _safe_calendar(session_factory, calendar, settings, now)
    news_inserted = await _safe_news(session_factory, news, settings, since, until)

    if events_inserted or news_inserted:
        logger.info(
            "content_cycle",
            events_inserted=events_inserted,
            news_inserted=news_inserted,
            since=since.isoformat(),
            until=until.isoformat(),
        )


async def _safe_calendar(
    session_factory: async_sessionmaker[AsyncSession],
    calendar: object,
    settings: Settings,
    now: datetime,
) -> int:
    """Fetch + persist calendar events; degrade to synthetic on runtime failure."""
    try:
        events = await calendar.fetch_events(  # type: ignore[attr-defined]
            since=now - timedelta(hours=settings.calendar_lookback_hours),
            until=now,
        )
    except ContentProviderError:
        logger.exception("calendar_provider_failed_degrading_to_synthetic")
        events = await _degraded_calendar(settings, now)
    async with session_factory() as session:
        inserted = await save_events(session, events)
        await session.commit()
    return inserted


async def _safe_news(
    session_factory: async_sessionmaker[AsyncSession],
    news: object,
    settings: Settings,
    since: datetime,
    until: datetime,
) -> int:
    """Fetch + persist news; degrade to synthetic on runtime failure."""
    try:
        items = await news.fetch_news(  # type: ignore[attr-defined]
            since=since,
            until=until,
        )
    except ContentProviderError:
        logger.exception("news_provider_failed_degrading_to_synthetic")
        items = await _degraded_news(settings, since, until)
    async with session_factory() as session:
        inserted = await save_news(session, items)
        await session.commit()
    return inserted


async def _degraded_calendar(settings: Settings, now: datetime) -> list[NormalizedEconomicEvent]:
    from app.data.providers.synthetic_calendar import SyntheticCalendarProvider

    provider = SyntheticCalendarProvider()
    events = await provider.fetch_events(
        since=now - timedelta(hours=settings.calendar_lookback_hours),
        until=now,
    )
    await provider.aclose()
    return events


async def _degraded_news(
    settings: Settings, since: datetime, until: datetime
) -> list[NormalizedNewsItem]:
    from app.data.providers.synthetic_news import SyntheticNewsProvider

    provider = SyntheticNewsProvider()
    items = await provider.fetch_news(since=since, until=until)
    await provider.aclose()
    return items


async def _sleep(seconds: float) -> None:  # pragma: no cover - thin alias for testability
    import asyncio

    await asyncio.sleep(seconds)
