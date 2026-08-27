"""Unit tests for the deterministic synthetic news/calendar content (Phase 4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.data.content_hash import calendar_dedup_key, news_item_hash
from app.data.content_types import IMPORTANCE_LEVELS
from app.data.providers.synthetic_calendar import SyntheticCalendarProvider
from app.data.providers.synthetic_content import (
    synthetic_calendar_events,
    synthetic_news_items,
)
from app.data.providers.synthetic_news import SyntheticNewsProvider

pytestmark = [pytest.mark.unit]

_RANGE = (
    datetime(2026, 8, 25, 0, 0, tzinfo=UTC),
    datetime(2026, 8, 26, 0, 0, tzinfo=UTC),
)


def test_calendar_generation_is_deterministic_and_order_independent() -> None:
    first = synthetic_calendar_events(*_RANGE)
    second = synthetic_calendar_events(*_RANGE)
    assert first == second
    # Interleaving another window must not perturb the first.
    _ = synthetic_calendar_events(
        *(datetime(2026, 9, 1, 0, 0, tzinfo=UTC), datetime(2026, 9, 3, 0, 0, tzinfo=UTC))
    )
    third = synthetic_calendar_events(*_RANGE)
    assert third == first


def test_calendar_events_have_stable_dedup_keys() -> None:
    events = synthetic_calendar_events(*_RANGE)
    assert events
    keys = {calendar_dedup_key(e) for e in events}
    assert len(keys) == len(events)


def test_calendar_events_respect_window() -> None:
    since, until = _RANGE
    for event in synthetic_calendar_events(since, until):
        assert since <= event.timestamp_utc < until


def test_calendar_events_are_utc_aware_and_valid_importance() -> None:
    for event in synthetic_calendar_events(*_RANGE):
        assert event.timestamp_utc.tzinfo is not None
        assert event.importance in IMPORTANCE_LEVELS
        assert event.currency in ("EUR", "GBP", "USD", "JPY", "AUD", "CAD", "CHF", "NZD")
        assert event.symbols


def test_calendar_empty_when_window_inverted() -> None:
    since, until = _RANGE
    assert synthetic_calendar_events(until, since) == []


def test_news_generation_is_deterministic() -> None:
    assert synthetic_news_items(*_RANGE) == synthetic_news_items(*_RANGE)


def test_news_hashes_are_stable_and_unique() -> None:
    items = synthetic_news_items(*_RANGE)
    assert items
    hashes = {news_item_hash(i) for i in items}
    assert len(hashes) == len(items)


def test_news_respect_window() -> None:
    since, until = _RANGE
    for item in synthetic_news_items(since, until):
        assert since <= item.published_utc < until


def test_news_empty_when_window_inverted() -> None:
    since, until = _RANGE
    assert synthetic_news_items(until, since) == []


async def test_synthetic_calendar_provider_implements_interface() -> None:
    provider = SyntheticCalendarProvider()
    try:
        events = await provider.fetch_events(since=_RANGE[0], until=_RANGE[1])
    finally:
        await provider.aclose()
    assert provider.name == "synthetic"
    assert events


async def test_synthetic_news_provider_implements_interface() -> None:
    provider = SyntheticNewsProvider()
    try:
        items = await provider.fetch_news(since=_RANGE[0], until=_RANGE[1])
    finally:
        await provider.aclose()
    assert provider.name == "synthetic"
    assert items
