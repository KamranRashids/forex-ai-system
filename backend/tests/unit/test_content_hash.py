"""Unit tests for deterministic idempotency keys (Phase 4, requirement #2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.data.content_hash import calendar_dedup_key, news_item_hash
from app.data.content_types import NormalizedEconomicEvent, NormalizedNewsItem

pytestmark = [pytest.mark.unit]

_TS = datetime(2026, 8, 27, 9, 30, tzinfo=UTC)


def _news(**overrides: object) -> NormalizedNewsItem:
    fields: dict[str, object] = {
        "provider": "synthetic",
        "headline": "  Sector Data Beats Forecasts: EUR  ",
        "published_utc": datetime(2026, 8, 27, 8, 0, tzinfo=UTC),
        "url": "https://demo.example/news/1000/1",
    }
    fields.update(overrides)
    return NormalizedNewsItem(**fields)  # type: ignore[arg-type]


def _event(**overrides: object) -> NormalizedEconomicEvent:
    fields: dict[str, object] = {
        "provider": "synthetic",
        "title": "EUR CPI YoY",
        "timestamp_utc": _TS,
        "importance": "high",
        "currency": "EUR",
        "external_id": None,
    }
    fields.update(overrides)
    return NormalizedEconomicEvent(**fields)  # type: ignore[arg-type]


def test_news_hash_is_stable_across_calls() -> None:
    assert news_item_hash(_news()) == news_item_hash(_news())


def test_news_hash_ignores_whitespace_and_case_in_headline_url() -> None:
    a = news_item_hash(
        _news(
            headline="  Sector Data Beats Forecasts: EUR  ",
            url="  https://demo.example/news/1000/1  ",
        )
    )
    b = news_item_hash(
        _news(headline="sector data beats forecasts: eur", url="https://demo.example/news/1000/1")
    )
    assert a == b


def test_news_hash_differentiates_two_distinct_items() -> None:
    assert news_item_hash(_news(headline="A")) != news_item_hash(_news(headline="B"))


def test_news_hash_includes_provider_to_prevent_collision() -> None:
    assert news_item_hash(_news(provider="synthetic")) != news_item_hash(_news(provider="finnhub"))


def test_news_hash_not_dependent_on_raw_payload_or_meta() -> None:
    with_meta = _news(raw_payload={"x": 1, "summary": "extra"})
    without = _news()
    assert news_item_hash(with_meta) == news_item_hash(without)


def test_calendar_dedup_key_uses_external_id_when_present() -> None:
    with_ext = _event(external_id="abc-123")
    same_ext = _event(external_id="abc-123")
    assert calendar_dedup_key(with_ext) == calendar_dedup_key(same_ext)


def test_calendar_dedup_key_differs_between_external_ids() -> None:
    assert calendar_dedup_key(_event(external_id="a")) != calendar_dedup_key(
        _event(external_id="b")
    )


def test_calendar_dedup_key_field_fallback_is_stable() -> None:
    assert calendar_dedup_key(_event(external_id=None)) == calendar_dedup_key(
        _event(external_id=None)
    )


def test_calendar_dedup_key_field_fallback_is_case_insensitive_title_currency() -> None:
    a = calendar_dedup_key(_event(external_id=None, title="  EUR CPI YoY ", currency="eur"))
    b = calendar_dedup_key(_event(external_id=None, title="eur cpi yoy", currency="EUR"))
    assert a == b
