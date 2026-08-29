"""Integration: Phase 4 content pipeline over real PostgreSQL.

Covers (requirements #2 idempotency, #3/#6 graceful degradation, #7 no-leak):
- migration 0004 + model/schema consistency for the two content tables;
- dedup/replay idempotency via the repositories (re-insert is a no-op);
- content worker degradation to synthetic on provider runtime failure;
- read API (auth, ordering, filters) over persisted normalized data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from app.data.content_repository import load_events, load_recent_news, save_events, save_news
from app.data.content_types import NormalizedEconomicEvent, NormalizedNewsItem
from app.data.providers.content_base import ContentProviderRateLimitError

pytestmark = [pytest.mark.integration]

PASSWORD = "correct-horse-battery-staple"


def _news_item(**overrides: object) -> NormalizedNewsItem:
    fields: dict[str, object] = {
        "provider": "synthetic",
        "headline": "CPI Report Beats Forecasts: EUR",
        "published_utc": datetime(2026, 8, 27, 9, 0, tzinfo=UTC),
        "url": "https://demo.example/news/1",
        "symbols": ("EURUSD",),
        "external_id": "syn-1",
    }
    fields.update(overrides)
    return NormalizedNewsItem(**fields)  # type: ignore[arg-type]


def _event(**overrides: object) -> NormalizedEconomicEvent:
    fields: dict[str, object] = {
        "provider": "synthetic",
        "title": "EUR CPI YoY",
        "timestamp_utc": datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
        "importance": "high",
        "currency": "EUR",
        "symbols": ("EURUSD",),
        "external_id": "syn-evt-1",
    }
    fields.update(overrides)
    return NormalizedEconomicEvent(**fields)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_migration_creates_content_tables_matching_models(pg_engine: Any) -> None:
    """The applied schema must equal the declared news/calendar models."""
    from app.models.economic_event import EconomicEvent
    from app.models.news_item import NewsItem
    from sqlalchemy import inspect as sa_inspect

    def compare(sync_conn: object) -> None:
        inspector = sa_inspect(sync_conn)
        db_tables = set(inspector.get_table_names())
        assert {"news_items", "economic_events"} <= db_tables
        for model in (NewsItem, EconomicEvent):
            table = model.__table__
            db_columns = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                assert column.name in db_columns, f"{table.name}.{column.name} missing"

    async with pg_engine.connect() as conn:
        await conn.run_sync(compare)


@pytest.mark.asyncio
async def test_alembic_head_is_content_migration(pg_engine: Any) -> None:
    """The DB is migrated to head, which includes revision 0006 (Phase 6)."""
    from pathlib import Path

    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    backend_root = Path(__file__).resolve().parents[2]
    script = ScriptDirectory.from_config(AlembicConfig(str(backend_root / "alembic.ini")))
    heads = set(script.get_heads())
    async with pg_engine.connect() as conn:
        current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    assert current in heads
    assert current == "0006"  # Phase 6 backtest tables are the current head


@pytest.mark.asyncio
async def test_save_news_replay_is_idempotent(db_sessionmaker: Any) -> None:
    item = _news_item()
    async with db_sessionmaker() as session:
        first = await save_news(session, [item])
        await session.commit()
        # Redelivering the same normalized item must not insert a duplicate.
        second = await save_news(session, [item])
        await session.commit()
        assert first == 1
        assert second == 0

        rows = await load_recent_news(session, limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row.headline == item.headline
        assert row.item_hash  # stable hash persisted, distinct from ingestion time


@pytest.mark.asyncio
async def test_save_events_replay_is_idempotent(db_sessionmaker: Any) -> None:
    event = _event()
    async with db_sessionmaker() as session:
        first = await save_events(session, [event])
        await session.commit()
        second = await save_events(session, [event])
        await session.commit()
        assert first == 1
        assert second == 0

        rows = await load_events(session, currencies=["EUR"], limit=10)
        assert len(rows) == 1
        assert rows[0].dedup_key


@pytest.mark.asyncio
async def test_content_worker_degrades_to_synthetic_on_failure(db_sessionmaker: Any) -> None:
    from app.core.config import Settings
    from app.workers.content_runtime import _safe_calendar, _safe_news

    class FailingCalendar:
        name = "failing"

        async def fetch_events(self, **kwargs: object) -> list[Any]:
            raise ContentProviderRateLimitError("boom")

    class FailingNews:
        name = "failing"

        async def fetch_news(self, **kwargs: object) -> list[Any]:
            raise ContentProviderRateLimitError("boom")

    settings = Settings(secret_key="s" * 40)
    # News lookback is 2h from `now`; `17:00Z` puts a synthesized headline
    # (16:00Z, UTC-normalized) strictly inside [15:00Z, 17:00Z) so the degraded
    # fallback must persist data regardless of the host's timezone.
    now = datetime(2026, 8, 27, 17, 0, tzinfo=UTC)

    events_inserted = await _safe_calendar(db_sessionmaker, FailingCalendar(), settings, now)
    news_inserted = await _safe_news(
        db_sessionmaker, FailingNews(), settings, now - timedelta(hours=2), now
    )
    # Degraded to synthetic -> data still persisted; loop never crashed.
    assert events_inserted > 0
    assert news_inserted > 0


@pytest.mark.asyncio
async def test_content_worker_persists_synthetic_through_cycle(db_sessionmaker: Any) -> None:
    """The full _cycle (provider -> repo) persists with dedup across calls."""
    from app.core.config import Settings
    from app.data.providers.factory import build_calendar, build_news
    from app.workers.content_runtime import _cycle

    settings = Settings(secret_key="s" * 40)  # default providers = synthetic
    calendar = build_calendar(settings)
    news = build_news(settings)
    try:
        await _cycle(db_sessionmaker, settings, calendar, news)
        await _cycle(db_sessionmaker, settings, calendar, news)  # replay -> no dup
    finally:
        await calendar.aclose()
        await news.aclose()

    async with db_sessionmaker() as session:
        news_rows = await load_recent_news(session, limit=1000)
        event_rows = await load_events(session, limit=1000)
        assert news_rows
        assert event_rows
        # Distinct stable hashes => no duplicate news rows after replay.
        hashes = {r.item_hash for r in news_rows}
        assert len(hashes) == len(news_rows)


@pytest.mark.asyncio
async def test_content_api_requires_auth(client: httpx.AsyncClient) -> None:
    anon_news = await client.get("/api/v1/news")
    assert anon_news.status_code == 401
    anon_events = await client.get("/api/v1/calendar/events")
    assert anon_events.status_code == 401


@pytest.mark.asyncio
async def test_content_api_returns_persisted_data(
    client: httpx.AsyncClient, db_sessionmaker: Any
) -> None:
    from tests.integration.conftest import bearer, register_and_login

    async with db_sessionmaker() as session:
        inserted = await save_news(
            session,
            [
                _news_item(
                    headline="First Story", published_utc=datetime(2026, 8, 27, 8, 0, tzinfo=UTC)
                ),
                _news_item(
                    headline="Second Story",
                    url="https://demo.example/news/2",
                    external_id="syn-2",
                    published_utc=datetime(2026, 8, 27, 9, 0, tzinfo=UTC),
                ),
            ],
        )
        await save_events(session, [_event()])
        await session.commit()
        assert inserted == 2

    tokens = await register_and_login(client, "content-viewer@example.com", PASSWORD)
    headers = bearer(tokens["access_token"])

    news = await client.get("/api/v1/news", headers=headers)
    assert news.status_code == 200
    body = news.json()
    assert len(body) == 2
    # Newest first ordering.
    assert body[0]["headline"] == "Second Story"
    assert body[0]["provider"] == "synthetic"
    assert "raw_payload" not in body[0]
    assert "item_hash" not in body[0]

    by_symbol = await client.get("/api/v1/news", params={"symbol": "EURUSD"}, headers=headers)
    assert by_symbol.status_code == 200
    assert len(by_symbol.json()) == 2

    events = await client.get("/api/v1/calendar/events", headers=headers)
    assert events.status_code == 200
    event_body = events.json()
    assert len(event_body) == 1
    assert event_body[0]["title"] == "EUR CPI YoY"
    assert event_body[0]["importance"] == "high"
    assert "raw_payload" not in event_body[0]

    by_currency = await client.get(
        "/api/v1/calendar/events", params={"currency": "EUR"}, headers=headers
    )
    assert len(by_currency.json()) == 1
    wrong_currency = await client.get(
        "/api/v1/calendar/events", params={"currency": "JPY"}, headers=headers
    )
    assert wrong_currency.json() == []


@pytest.mark.asyncio
async def test_content_api_rejects_bad_params(client: httpx.AsyncClient) -> None:
    from tests.integration.conftest import bearer, register_and_login

    tokens = await register_and_login(client, "content-viewer2@example.com", PASSWORD)
    headers = bearer(tokens["access_token"])
    bad_limit = await client.get("/api/v1/news", params={"limit": 9999}, headers=headers)
    assert bad_limit.status_code == 422
    bad_symbol = await client.get(
        "/api/v1/news", params={"symbol": "NOTAVALIDPAIR"}, headers=headers
    )
    assert bad_symbol.status_code == 422
