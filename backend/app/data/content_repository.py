"""Normalized news/calendar persistence with deduplication (Phase 4).

Replay safety (implementation requirement #5): persistence is keyed on
deterministic hashes — ``item_hash`` for news, ``dedup_key`` for calendar —
via ``ON CONFLICT DO NOTHING``. Re-polling the same provider window (or a
redelivered batch) inserts zero duplicate rows.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.data.content_hash import calendar_dedup_key, news_item_hash
from app.data.content_types import NormalizedEconomicEvent, NormalizedNewsItem
from app.models.economic_event import EconomicEvent
from app.models.news_item import NewsItem


def _news_values(item: NormalizedNewsItem) -> dict[str, object]:
    return {
        "item_hash": news_item_hash(item),
        "provider": item.provider,
        "external_id": item.external_id,
        "url": item.url,
        "headline": item.headline,
        "summary": item.summary,
        "symbols": list(item.symbols),
        "published_utc": item.published_utc,
        "raw_payload": item.raw_payload,
    }


def _event_values(event: NormalizedEconomicEvent) -> dict[str, object]:
    from decimal import Decimal

    surprise = None
    if event.surprise_score is not None:
        surprise = Decimal(str(event.surprise_score))
    return {
        "dedup_key": calendar_dedup_key(event),
        "provider": event.provider,
        "external_id": event.external_id,
        "title": event.title,
        "currency": event.currency,
        "symbols": list(event.symbols),
        "importance": event.importance,
        "timestamp_utc": event.timestamp_utc,
        "actual": event.actual,
        "forecast": event.forecast,
        "previous": event.previous,
        "surprise_score": surprise,
        "raw_payload": event.raw_payload,
    }


async def save_news(session: AsyncSession, items: list[NormalizedNewsItem]) -> int:
    """Insert news items; replays of the same item_hash are no-ops.

    Returns the number of freshly inserted rows.
    """
    inserted = 0
    for item in items:
        stmt = (
            pg_insert(NewsItem)
            .values(_news_values(item))
            .on_conflict_do_nothing(index_elements=["item_hash"])
            .returning(NewsItem.id)
        )
        result = await session.scalar(stmt)
        if result is not None:
            inserted += 1
    return inserted


async def save_events(session: AsyncSession, events: list[NormalizedEconomicEvent]) -> int:
    """Insert calendar events; replays of the same dedup_key are no-ops.

    Returns the number of freshly inserted rows.
    """
    inserted = 0
    for event in events:
        stmt = (
            pg_insert(EconomicEvent)
            .values(_event_values(event))
            .on_conflict_do_nothing(index_elements=["dedup_key"])
            .returning(EconomicEvent.id)
        )
        result = await session.scalar(stmt)
        if result is not None:
            inserted += 1
    return inserted


async def load_recent_news(
    session: AsyncSession,
    *,
    symbols: list[str] | None = None,
    limit: int = 100,
    since: datetime | None = None,
) -> list[NewsItem]:
    """Most recent news items, newest first, optional symbols/time filter."""
    conditions: list[ColumnElement[bool]] = []
    if symbols:
        conditions.append(NewsItem.symbols.overlap(symbols))
    if since is not None:
        conditions.append(NewsItem.published_utc >= since)
    result = await session.execute(
        select(NewsItem).where(*conditions).order_by(NewsItem.published_utc.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def load_events(
    session: AsyncSession,
    *,
    currencies: list[str] | None = None,
    limit: int = 100,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[EconomicEvent]:
    """Economic events ordered by timestamp, optional currency/window filter."""
    conditions: list[ColumnElement[bool]] = []
    if currencies:
        conditions.append(EconomicEvent.currency.in_(currencies))
    if since is not None:
        conditions.append(EconomicEvent.timestamp_utc >= since)
    if until is not None:
        conditions.append(EconomicEvent.timestamp_utc < until)
    result = await session.execute(
        select(EconomicEvent)
        .where(*conditions)
        .order_by(EconomicEvent.timestamp_utc.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
