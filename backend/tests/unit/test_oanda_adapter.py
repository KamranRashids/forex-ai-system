"""Adapter contract tests for the OANDA practice provider (respx-mocked HTTP)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx
from app.data.providers.base import ProviderError, ProviderTransientError
from app.data.providers.oanda import (
    OandaPracticeProvider,
    parse_oanda_time,
    to_oanda_symbol,
)

pytestmark = [pytest.mark.unit]

_TOKEN = "test-token-not-a-real-credential"
_WINDOW = (
    datetime(2026, 8, 21, 10, 0, tzinfo=UTC),
    datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
)


def _candles_payload() -> dict:
    return {
        "instrument": "EUR_USD",
        "granularity": "M15",
        "candles": [
            {
                "time": "2026-08-21T10:00:00.000000000Z",
                "volume": 1234,
                "complete": True,
                "mid": {"o": "1.08501", "h": "1.08602", "l": "1.08403", "c": "1.08555"},
            },
            {
                # Outside requested window (>= end) -> filtered out.
                "time": "2026-08-21T12:00:00.000000000Z",
                "volume": 10,
                "complete": True,
                "mid": {"o": "1.08500", "h": "1.08500", "l": "1.08490", "c": "1.08495"},
            },
            {
                # Forming bar -> dropped (contract: closed bars only).
                "time": "2026-08-21T11:45:00.000000000Z",
                "volume": 3,
                "complete": False,
                "mid": {"o": "1.08520", "h": "1.08530", "l": "1.08510", "c": "1.08525"},
            },
            {
                "time": "2026-08-21T10:15:00.123456789Z",
                "volume": 1500,
                "complete": True,
                "mid": {"o": "1.08560", "h": "1.08700", "l": "1.08550", "c": "1.08680"},
            },
        ],
    }


def test_symbol_mapping() -> None:
    assert to_oanda_symbol("EURUSD") == "EUR_USD"
    assert to_oanda_symbol("USDJPY") == "USD_JPY"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-21T10:00:00.000000000Z", datetime(2026, 8, 21, 10, 0, tzinfo=UTC)),
        ("2026-08-21T10:15:00.123456789Z", datetime(2026, 8, 21, 10, 15, 0, 123456, UTC)),
        ("2026-08-21T10:00:00Z", datetime(2026, 8, 21, 10, 0, tzinfo=UTC)),
    ],
)
def test_time_parsing_handles_nanoseconds(raw: str, expected: datetime) -> None:
    assert parse_oanda_time(raw) == expected


def test_missing_token_refuses_construction() -> None:
    with pytest.raises(ValueError, match="OANDA_API_TOKEN is empty"):
        OandaPracticeProvider(api_token="")


@respx.mock
async def test_fetch_candles_maps_and_filters_correctly() -> None:
    route = respx.get("https://api-fxpractice.oanda.com/v3/instruments/EUR_USD/candles")
    route.respond(200, json=_candles_payload())

    provider = OandaPracticeProvider(api_token=_TOKEN)
    try:
        start, end = _WINDOW
        candles = await provider.fetch_candles(
            symbol="EURUSD", timeframe="M15", start=start, end=end
        )
    finally:
        await provider.aclose()

    assert route.called
    request = route.calls.last.request
    assert request.url.params["granularity"] == "M15"
    assert request.url.params["price"] == "M"

    assert len(candles) == 2  # outside-window and forming bars removed
    first = candles[0]
    assert first.bucket_start == datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    assert first.open == Decimal("1.08501")
    assert first.high == Decimal("1.08602")
    assert first.low == Decimal("1.08403")
    assert first.close == Decimal("1.08555")
    assert first.volume == 1234
    assert first.complete is True


@respx.mock
async def test_transient_5xx_is_retried_then_succeeds() -> None:
    route = respx.get("https://api-fxpractice.oanda.com/v3/instruments/EUR_USD/candles")
    route.side_effect = [
        httpx.Response(500, json={}),
        httpx.Response(200, json=_candles_payload()),
    ]
    provider = OandaPracticeProvider(api_token=_TOKEN)
    try:
        start, end = _WINDOW
        candles = await provider.fetch_candles(
            symbol="EURUSD", timeframe="M15", start=start, end=end
        )
        assert len(candles) == 2
        assert route.call_count == 2
    finally:
        await provider.aclose()


@respx.mock
async def test_permanent_401_raises_provider_error_without_retry() -> None:
    route = respx.get("https://api-fxpractice.oanda.com/v3/instruments/EUR_USD/candles")
    route.respond(401, json={"errorMessage": "Invalid bearer token"})

    provider = OandaPracticeProvider(api_token=_TOKEN)
    try:
        start, end = _WINDOW
        with pytest.raises(ProviderError, match="HTTP 401"):
            await provider.fetch_candles(symbol="EURUSD", timeframe="M15", start=start, end=end)
        assert route.call_count == 1  # no retry on permanent failures
    finally:
        await provider.aclose()


@respx.mock
async def test_exhausted_retries_surface_transient_error() -> None:
    route = respx.get("https://api-fxpractice.oanda.com/v3/instruments/EUR_USD/candles")
    route.mock(side_effect=httpx.ConnectError("connection refused"))

    provider = OandaPracticeProvider(api_token=_TOKEN)
    try:
        start, end = _WINDOW
        with pytest.raises(ProviderTransientError):
            await provider.fetch_candles(symbol="EURUSD", timeframe="M15", start=start, end=end)
        assert route.call_count == 3  # stop_after_attempt(3)
    finally:
        await provider.aclose()


_STREAM_BODY = "\n".join(
    [
        "",
        'HEARTBEAT {"type":"HEARTBEAT"}',
        "SNAPSHOT {bad json",
        'PRICE {"type":"PRICE","bids":[{"price":"1.08500"}],"asks":[{"price":"1.08512"}],'
        '"instrument":"EUR_USD","time":"2026-08-21T10:00:00.512345789Z"}',
        'PRICE {"type":"PRICE","bids":[],"asks":[],"instrument":"JUNK_PAIR","time":"bogus"}',
        'PRICE {"type":"PRICE","bids":[{"price":"155.250"}],"asks":[{"price":"155.270"}],'
        '"instrument":"USD_JPY","time":"2026-08-21T10:00:01.000000000Z"}',
    ]
)


@respx.mock
async def test_stream_parses_ticks_and_skips_poison_lines() -> None:
    route = respx.get("https://stream-fxpractice.oanda.com/v3/prices/stream")
    route.respond(
        200,
        content=_STREAM_BODY.encode(),
        headers={"content-type": "application/json"},
    )

    provider = OandaPracticeProvider(api_token=_TOKEN)
    ticks = []
    try:
        async for tick in provider.stream_quotes(["EURUSD", "USDJPY"]):
            ticks.append(tick)
    finally:
        await provider.aclose()

    assert len(ticks) == 2  # heartbeat + malformed lines skipped
    eur = ticks[0]
    assert eur.symbol == "EURUSD"
    assert eur.bid == Decimal("1.08500")
    assert eur.ask == Decimal("1.08512")
    assert eur.ts.microsecond == 512345
    jpy = ticks[1]
    assert jpy.symbol == "USDJPY"
    assert jpy.bid == Decimal("155.250")


@respx.mock
async def test_stream_http_error_raises_transient() -> None:
    respx.get("https://stream-fxpractice.oanda.com/v3/prices/stream").respond(503)
    provider = OandaPracticeProvider(api_token=_TOKEN)
    try:
        with pytest.raises(ProviderTransientError, match="HTTP 503"):
            async for _tick in provider.stream_quotes(["EURUSD"]):
                pass
    finally:
        await provider.aclose()


async def test_factory_builds_synthetic_by_default() -> None:
    from app.core.config import Settings
    from app.data.providers.factory import build_provider

    provider = build_provider(Settings(secret_key="s" * 40))
    assert provider.name == "synthetic"


async def test_factory_refuses_unknown_provider() -> None:
    from app.core.config import Settings
    from app.data.providers.base import ProviderError as PE
    from app.data.providers.factory import build_provider

    settings = Settings(secret_key="s" * 40)
    object.__setattr__(settings, "market_data_provider", "unknown")  # bypass validator
    with pytest.raises(PE):
        build_provider(settings)
