"""Market data read API (Phase 12 O-1): candles + latest price quotes.

Viewer+ read endpoints. ``/candles`` reads the PostgreSQL candle store;
``/prices/latest`` reads the Redis latest-price cache written by the ingest
worker. Both are strictly read-only — no provider I/O, no order path (SAFE
MODE preserved).
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DBSession, SettingsDep
from app.bus.topics import latest_price_key
from app.core.errors import InvalidInputError, NotFoundError, ServiceUnavailableError
from app.data.repository import load_candles
from app.db.session import get_redis
from app.models.candle import CandleRow
from app.models.instrument import Instrument
from app.schemas.market import CandleOut, LatestPriceOut

router = APIRouter(prefix="/market", tags=["market"])

RedisDep = Annotated[Redis, Depends(get_redis)]

_TIMEFRAME_PATTERN = "^(M5|M15|H1|H4|D1)$"


def _parse_price(symbol: str, raw: str) -> LatestPriceOut | None:
    """Parse a ``prices.latest:{symbol}`` cache entry; skip malformed rows."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    price = data.get("price")
    if price is None:
        return None
    try:
        parsed_price = Decimal(str(price))
    except (TypeError, ValueError, InvalidOperation):
        return None
    bucket_start = data.get("bucket_start")
    if isinstance(bucket_start, str):
        try:
            parsed_bucket = datetime.fromisoformat(bucket_start)
        except ValueError:
            parsed_bucket = None
    else:
        parsed_bucket = None
    return LatestPriceOut(
        symbol=symbol,
        price=parsed_price,
        bucket_start=parsed_bucket,
        synthetic=bool(data.get("synthetic", False)),
    )


def _candle_out(symbol: str, row: CandleRow) -> CandleOut:
    return CandleOut(
        symbol=symbol,
        timeframe=row.timeframe,
        ts=row.ts,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
    )


async def _resolve_instrument(session: AsyncSession, symbol: str) -> Instrument:
    instrument = await session.scalar(select(Instrument).where(Instrument.symbol == symbol))
    if instrument is None:
        raise NotFoundError(f"Instrument {symbol!r} not found")
    return instrument


@router.get("/candles", response_model=list[CandleOut])
async def list_candles(
    session: DBSession,
    current: CurrentUser,
    symbol: Annotated[str, Query(min_length=6, max_length=12)],
    timeframe: Annotated[str, Query(pattern=_TIMEFRAME_PATTERN)],
    start: datetime | None = None,
    end: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> list[CandleOut]:
    """Stored closed candles ascending for a pair/timeframe (viewer+)."""
    if start is not None and end is not None and start >= end:
        raise InvalidInputError("start must be earlier than end")
    symbol = symbol.upper()
    timeframe = timeframe.upper()
    instrument = await _resolve_instrument(session, symbol)
    rows = await load_candles(
        session,
        instrument_id=instrument.id,
        timeframe=timeframe,
        start=start,
        end=end,
        limit=limit,
    )
    return [_candle_out(symbol, row) for row in rows]


@router.get("/prices/latest", response_model=list[LatestPriceOut])
async def latest_prices(
    redis: RedisDep,
    current: CurrentUser,
    settings: SettingsDep,
    symbol: Annotated[str | None, Query(min_length=6, max_length=12)] = None,
) -> list[LatestPriceOut]:
    """Newest cached price for one symbol or every configured symbol (viewer+)."""
    symbols = [symbol.upper()] if symbol else list(settings.market_symbols)
    quotes: list[LatestPriceOut] = []
    try:
        for sym in symbols:
            raw = await redis.get(latest_price_key(sym))
            if raw is None:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            quote = _parse_price(sym, raw)
            if quote is not None:
                quotes.append(quote)
    except RedisError as exc:
        raise ServiceUnavailableError("Price cache unavailable") from exc
    return quotes
