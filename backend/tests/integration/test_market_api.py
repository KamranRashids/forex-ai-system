"""Integration: market read API (Phase 12 O-1) — candles + latest prices."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.bus.topics import latest_price_key
from app.data.ingest import seed_instruments
from app.data.providers.synthetic import synthetic_candles
from app.data.repository import upsert_candles

pytestmark = [pytest.mark.integration]


def _friday_noon() -> datetime:
    return datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


async def _seed_candles(db_sessionmaker: Any) -> None:
    start = _friday_noon() - timedelta(hours=24)
    end = _friday_noon() + timedelta(hours=8)
    candles = synthetic_candles("EURUSD", "M15", start, end)
    async with db_sessionmaker() as session:
        instrument = await seed_instruments(session, ["EURUSD"])
        await session.commit()
        await upsert_candles(
            session,
            instrument_id=instrument["EURUSD"].id,
            candles=candles,
            source="synthetic",
            timeframe="M15",
        )
        await session.commit()


async def _auth_headers(client: Any) -> dict[str, str]:
    from tests.integration.conftest import bearer, register_and_login

    token = await register_and_login(client, "market@example.com")
    return bearer(token["access_token"])


async def test_candles_requires_auth(client: Any, db_sessionmaker: Any) -> None:
    await _seed_candles(db_sessionmaker)
    resp = await client.get("/api/v1/market/candles?symbol=EURUSD&timeframe=M15")
    assert resp.status_code == 401


async def test_candles_returns_ascending_rows(client: Any, db_sessionmaker: Any) -> None:
    await _seed_candles(db_sessionmaker)
    headers = await _auth_headers(client)
    resp = await client.get(
        "/api/v1/market/candles?symbol=EURUSD&timeframe=M15&limit=10", headers=headers
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 10
    first = rows[0]
    assert set(first) == {"symbol", "timeframe", "ts", "open", "high", "low", "close", "volume"}
    assert first["symbol"] == "EURUSD"
    assert first["timeframe"] == "M15"
    timestamps = [r["ts"] for r in rows]
    assert timestamps == sorted(timestamps)
    assert all(float(r["open"]) > 0 for r in rows)


async def test_candles_honors_time_window(client: Any, db_sessionmaker: Any) -> None:
    await _seed_candles(db_sessionmaker)
    headers = await _auth_headers(client)
    start = (_friday_noon() - timedelta(hours=4)).isoformat()
    end = _friday_noon().isoformat()
    resp = await client.get(
        "/api/v1/market/candles",
        params={"symbol": "EURUSD", "timeframe": "M15", "start": start, "end": end, "limit": 1000},
        headers=headers,
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 16  # 4 hours of M15 bars
    assert all(start < r["ts"] < end for r in rows)


async def test_candles_unknown_symbol_is_404(client: Any, db_sessionmaker: Any) -> None:
    headers = await _auth_headers(client)
    resp = await client.get("/api/v1/market/candles?symbol=XXXYYY&timeframe=M15", headers=headers)
    assert resp.status_code == 404
    body = resp.json()
    assert body["type"].endswith("/problems/not_found")


async def test_candles_invalid_timeframe_is_422(client: Any, db_sessionmaker: Any) -> None:
    await _seed_candles(db_sessionmaker)
    headers = await _auth_headers(client)
    resp = await client.get("/api/v1/market/candles?symbol=EURUSD&timeframe=M50", headers=headers)
    assert resp.status_code == 422


async def test_candles_start_after_end_is_400(client: Any, db_sessionmaker: Any) -> None:
    await _seed_candles(db_sessionmaker)
    headers = await _auth_headers(client)
    start = _friday_noon().isoformat()
    end = (_friday_noon() - timedelta(hours=1)).isoformat()
    resp = await client.get(
        "/api/v1/market/candles",
        params={"symbol": "EURUSD", "timeframe": "M15", "start": start, "end": end},
        headers=headers,
    )
    assert resp.status_code == 400


async def test_prices_latest_requires_auth(client: Any) -> None:
    resp = await client.get("/api/v1/market/prices/latest")
    assert resp.status_code == 401


async def test_prices_latest_returns_cached_quotes(client: Any, fake_redis: Any) -> None:
    await fake_redis.set(
        latest_price_key("EURUSD"),
        json.dumps(
            {
                "symbol": "EURUSD",
                "price": 1.0856,
                "bucket_start": "2026-08-21T12:45:00.000000Z",
                "synthetic": True,
            }
        ),
    )
    await fake_redis.set(
        latest_price_key("GBPUSD"),
        json.dumps({"symbol": "GBPUSD", "price": 1.2711, "synthetic": False}),
    )
    headers = await _auth_headers(client)
    resp = await client.get("/api/v1/market/prices/latest", headers=headers)
    assert resp.status_code == 200
    rows = {r["symbol"]: r for r in resp.json()}
    assert float(rows["EURUSD"]["price"]) == 1.0856
    assert rows["EURUSD"]["synthetic"] is True
    assert float(rows["GBPUSD"]["price"]) == 1.2711
    assert rows["GBPUSD"]["bucket_start"] is None


async def test_prices_latest_skips_malformed_and_missing(client: Any, fake_redis: Any) -> None:
    await fake_redis.set(latest_price_key("EURUSD"), "not-json")
    await fake_redis.set(latest_price_key("GBPUSD"), json.dumps({"symbol": "GBPUSD"}))
    headers = await _auth_headers(client)
    resp = await client.get("/api/v1/market/prices/latest", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []


async def test_prices_latest_symbol_filter(client: Any, fake_redis: Any) -> None:
    await fake_redis.set(
        latest_price_key("EURUSD"),
        json.dumps({"symbol": "EURUSD", "price": 1.0856, "synthetic": True}),
    )
    await fake_redis.set(
        latest_price_key("GBPUSD"),
        json.dumps({"symbol": "GBPUSD", "price": 1.2711, "synthetic": False}),
    )
    headers = await _auth_headers(client)
    resp = await client.get("/api/v1/market/prices/latest?symbol=EURUSD", headers=headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["symbol"] for r in rows] == ["EURUSD"]
