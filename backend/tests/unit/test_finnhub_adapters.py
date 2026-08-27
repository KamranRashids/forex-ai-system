"""Unit tests for Finnhub news/calendar adapters (respx-mocked HTTP).

Exercise normalization (incl. malformed/injection-shaped payloads), the token
never leaking into normalized objects, and the client's error classification /
retry rules. SAFE MODE: read-only content access; no order path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from app.data.providers.content_base import (
    ContentProviderAuthError,
    ContentProviderRateLimitError,
    ContentProviderTransientError,
)
from app.data.providers.finnhub_calendar import (
    FinnhubCalendarProvider,
    normalize_event,
)
from app.data.providers.finnhub_client import FinnhubClient
from app.data.providers.finnhub_news import FinnhubNewsProvider, normalize_item

pytestmark = [pytest.mark.unit]

_TOKEN = "test-token-never-persisted"
_WINDOW = (
    datetime(2026, 8, 27, 0, 0, tzinfo=UTC),
    datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
)
_IN_WINDOW_TS = int(datetime(2026, 8, 27, 9, 0, tzinfo=UTC).timestamp())


_BASE = "https://finnhub.io/api/v1"


def _get_url(path: str) -> str:
    return f"{_BASE}{path}"


# --- news normalization ------------------------------------------------------


def test_normalize_news_maps_stable_fields() -> None:
    item = {
        "id": 123,
        "headline": "  EUR rallies after hawkish ECB  ",
        "datetime": 1756000000,
        "url": "https://demo.example/a",
        "summary": "  Something happened.  ",
        "related": "EURUSD,USDJPY",
        "source": "demo",
    }
    norm = normalize_item(item)
    assert norm is not None
    assert norm.provider == "finnhub"
    assert norm.headline == "EUR rallies after hawkish ECB"
    assert norm.published_utc.tzinfo is not None
    assert norm.summary == "Something happened."
    assert norm.symbols == ("EURUSD", "USDJPY")
    assert norm.external_id == "123"
    assert norm.url == "https://demo.example/a"


def test_normalize_news_drops_missing_headline() -> None:
    assert normalize_item({"datetime": 1756000000}) is None


def test_normalize_news_drops_bad_datetime() -> None:
    assert normalize_item({"headline": "x", "datetime": "not-a-ts"}) is None
    assert normalize_item({"headline": "x", "datetime": True}) is None


def test_normalize_news_symbols_filter_to_fx_shape() -> None:
    norm = normalize_item(
        {"headline": "x", "datetime": 1756000000, "related": "AAPL, EURUSD, NOTACURRENCY"}
    )
    assert norm is not None
    assert norm.symbols == ("EURUSD",)


def test_normalize_news_non_dict_returns_none() -> None:
    assert normalize_item("junk") is None
    assert normalize_item([1, 2]) is None


def test_normalize_news_never_contains_provider_token() -> None:
    item = {
        "id": 1,
        "headline": "ok",
        "datetime": 1756000000,
        "url": "https://demo/a",
        "token": _TOKEN,
        "Authorization": _TOKEN,
    }
    norm = normalize_item(item)
    assert norm is not None
    assert _TOKEN not in str(norm.raw_payload)
    assert "token" not in norm.raw_payload


# --- calendar normalization ---------------------------------------------------


def test_normalize_calendar_maps_stable_fields() -> None:
    item = {
        "id": 77,
        "event": "CPI YoY",
        "country": "us",
        "currency": "usd",
        "date": "2026-08-27 08:00:00",
        "actual": "3.2%",
        "forecast": "3.1%",
        "prev": "3.0%",
    }
    norm = normalize_event(item)
    assert norm is not None
    assert norm.provider == "finnhub"
    assert norm.title == "CPI YoY"
    assert norm.importance == "high"  # CPI classified high
    assert norm.currency == "USD"
    assert norm.actual == "3.2%"
    assert norm.forecast == "3.1%"
    assert norm.previous == "3.0%"
    assert norm.external_id == "77"


def test_normalize_calendar_defaults_importance_to_medium() -> None:
    norm = normalize_event({"id": 1, "event": "Some Obscure Metric", "date": "2026-08-27 08:00:00"})
    assert norm is not None
    assert norm.importance == "medium"


def test_normalize_calendar_drops_missing_title_or_date() -> None:
    assert normalize_event({"id": 1, "date": "2026-08-27 08:00:00"}) is None
    assert normalize_event({"id": 1, "event": "x"}) is None


def test_normalize_calendar_bad_date_returns_none() -> None:
    assert normalize_event({"id": 1, "event": "x", "date": "not-a-date"}) is None


def test_normalize_calendar_invalid_currency_becomes_empty() -> None:
    norm = normalize_event({"id": 1, "event": "x", "date": "2026-08-27 08:00:00", "currency": "X"})
    assert norm is not None
    assert norm.currency == ""


def test_normalize_calendar_never_contains_provider_token() -> None:
    norm = normalize_event(
        {
            "id": 1,
            "event": "x",
            "date": "2026-08-27 08:00:00",
            "token": _TOKEN,
            "apikey": _TOKEN,
        }
    )
    assert norm is not None
    assert _TOKEN not in str(norm.raw_payload)


# --- client error classification + retries -----------------------------------


def test_client_constructing_with_empty_token_raises_auth() -> None:
    with pytest.raises(ContentProviderAuthError):
        FinnhubClient(api_token="")


@respx.mock
async def test_news_fetch_maps_and_filters_window() -> None:
    route = respx.get(_get_url("/news"))
    route.respond(
        200,
        json=[
            {"id": 1, "headline": "in window", "datetime": _IN_WINDOW_TS, "related": "EURUSD"},
            # 30 days before the window -> outside look-back -> filtered
            {
                "id": 2,
                "headline": "old",
                "datetime": int((datetime(2026, 7, 28, 9, 0, tzinfo=UTC)).timestamp()),
                "related": "EURUSD",
            },
        ],
    )
    provider = FinnhubNewsProvider(api_token=_TOKEN)
    try:
        items = await provider.fetch_news(since=_WINDOW[0], until=_WINDOW[1])
    finally:
        await provider.aclose()
    assert route.called
    assert "token" in dict(route.calls.last.request.url.params)
    assert [i.external_id for i in items] == ["1"]


@respx.mock
async def test_calendar_fetch_uses_from_to_params() -> None:
    route = respx.get(_get_url("/calendar/economic"))
    route.respond(
        200,
        json={
            "economicCalendar": [
                {"id": 9, "event": "GDP QoQ", "date": "2026-08-27 12:00:00", "currency": "EUR"}
            ]
        },
    )
    provider = FinnhubCalendarProvider(api_token=_TOKEN)
    try:
        events = await provider.fetch_events(since=_WINDOW[0], until=_WINDOW[1])
    finally:
        await provider.aclose()
    assert route.called
    params = dict(route.calls.last.request.url.params)
    assert params["from"] == "2026-08-27"
    assert params["to"] == "2026-08-28"
    assert [e.external_id for e in events] == ["9"]


@respx.mock
async def test_401_raises_auth_without_retry() -> None:
    route = respx.get(_get_url("/news")).respond(401, json={})
    provider = FinnhubNewsProvider(api_token=_TOKEN)
    try:
        with pytest.raises(ContentProviderAuthError):
            await provider.fetch_news(since=_WINDOW[0], until=_WINDOW[1])
    finally:
        await provider.aclose()
    assert route.call_count == 1


@respx.mock
async def test_429_raises_rate_limit_without_retry() -> None:
    route = respx.get(_get_url("/news")).respond(429, json={})
    provider = FinnhubNewsProvider(api_token=_TOKEN)
    try:
        with pytest.raises(ContentProviderRateLimitError):
            await provider.fetch_news(since=_WINDOW[0], until=_WINDOW[1])
    finally:
        await provider.aclose()
    assert route.call_count == 1


@respx.mock
async def test_5xx_is_retried_then_succeeds() -> None:
    route = respx.get(_get_url("/news"))
    route.side_effect = [
        httpx.Response(500, json={}),
        httpx.Response(200, json=[]),
    ]
    provider = FinnhubNewsProvider(api_token=_TOKEN)
    try:
        items = await provider.fetch_news(since=_WINDOW[0], until=_WINDOW[1])
    finally:
        await provider.aclose()
    assert items == []
    assert route.call_count == 2


@respx.mock
async def test_network_error_retried_then_transient() -> None:
    route = respx.get(_get_url("/news"))
    route.side_effect = httpx.ConnectError("boom")
    provider = FinnhubNewsProvider(api_token=_TOKEN)
    try:
        with pytest.raises(ContentProviderTransientError):
            await provider.fetch_news(since=_WINDOW[0], until=_WINDOW[1])
    finally:
        await provider.aclose()
    assert route.call_count == 3  # stop_after_attempt(3)
