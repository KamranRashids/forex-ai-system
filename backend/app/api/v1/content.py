"""News + economic-calendar read API (Phase 4; UI-ready payloads).

Viewer+ endpoints. Responses are read-only aggregation of normalized internal
data — no provider I/O, no order path (SAFE MODE).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DBSession
from app.data.content_repository import load_events, load_recent_news
from app.schemas.content import EconomicEventOut, NewsOut

router = APIRouter(tags=["content"])


def _news_out(row: Any) -> NewsOut:
    return NewsOut.model_validate(row, from_attributes=True)


def _event_out(row: Any) -> EconomicEventOut:
    return EconomicEventOut.model_validate(row, from_attributes=True)


@router.get("/news", response_model=list[NewsOut])
async def list_news(
    session: DBSession,
    current: CurrentUser,
    symbol: Annotated[str | None, Query(min_length=6, max_length=12)] = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[NewsOut]:
    """Recent normalized news items, newest first (viewer+)."""
    symbols = [symbol] if symbol else None
    rows = await load_recent_news(session, symbols=symbols, limit=limit, since=since)
    return [_news_out(r) for r in rows]


@router.get("/calendar/events", response_model=list[EconomicEventOut])
async def list_events(
    session: DBSession,
    current: CurrentUser,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[EconomicEventOut]:
    """Recent economic-calendar events ordered by time (viewer+)."""
    currencies = [currency] if currency else None
    rows = await load_events(session, currencies=currencies, limit=limit, since=since, until=until)
    return [_event_out(r) for r in rows]
