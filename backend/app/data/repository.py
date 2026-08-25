"""Market-data persistence: instruments seeding + idempotent candle upserts."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.providers.base import Candle
from app.data.timeframes import Timeframe, align_to_bucket
from app.models.candle import CandleRow
from app.models.instrument import Instrument, pip_size_for, price_decimals_for


async def get_or_create_instrument(session: AsyncSession, symbol: str) -> Instrument:
    """Fetch or seed an instrument row for ``symbol`` (e.g. "EURUSD")."""
    normalized = symbol.strip().upper()
    existing = await session.scalar(select(Instrument).where(Instrument.symbol == normalized))
    if existing is not None:
        return existing

    instrument = Instrument(
        symbol=normalized,
        base=normalized[:3],
        quote=normalized[3:6],
        pip_size=pip_size_for(normalized),
        price_decimals=price_decimals_for(normalized),
        active=True,
    )
    session.add(instrument)
    await session.flush()
    return instrument


async def ensure_instruments(session: AsyncSession, symbols: list[str]) -> dict[str, Instrument]:
    """Seed/fetch every symbol; returns a symbol -> Instrument mapping."""
    return {symbol: await get_or_create_instrument(session, symbol) for symbol in symbols}


async def list_active_instruments(session: AsyncSession) -> list[Instrument]:
    result = await session.execute(select(Instrument).where(Instrument.active.is_(True)))
    return list(result.scalars().all())


async def upsert_candles(
    session: AsyncSession,
    *,
    instrument_id: uuid.UUID,
    candles: list[Candle],
    source: str,
    timeframe: str,
) -> tuple[int, int]:
    """Insert-or-refresh closed bars. Returns (inserted, updated).

    Idempotent by construction: re-ingesting the same bars updates nothing
    observable except source/complete refreshes on identical values.
    A stored complete bar is never downgraded to incomplete.
    """
    if not candles:
        return 0, 0

    tf_minutes = Timeframe.seconds(timeframe) // 60
    unique_candles: dict[datetime, Candle] = {}
    for candle in candles:
        bucket = align_to_bucket(candle.bucket_start, timeframe)
        unique_candles.setdefault(bucket, candle)

    rows_before = await count_candles(session, instrument_id=instrument_id, timeframe=timeframe)

    for bucket, candle in unique_candles.items():
        await session.execute(
            pg_insert(CandleRow)
            .values(
                instrument_id=instrument_id,
                timeframe=timeframe,
                ts=bucket,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                source=source,
                complete=candle.complete,
                tf_minutes=tf_minutes,
            )
            .on_conflict_do_update(
                index_elements=["instrument_id", "timeframe", "ts"],
                set_={
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                    "source": source,
                    # Complete never regresses.
                    "complete": CandleRow.complete | candle.complete,
                },
            )
        )

    rows_after = await count_candles(session, instrument_id=instrument_id, timeframe=timeframe)
    inserted = max(0, rows_after - rows_before)
    updated = len(unique_candles) - inserted
    return inserted, max(0, updated)


async def count_candles(session: AsyncSession, *, instrument_id: uuid.UUID, timeframe: str) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(CandleRow)
        .where(CandleRow.instrument_id == instrument_id, CandleRow.timeframe == timeframe)
    )
    return int(total or 0)


async def last_closed_ts(
    session: AsyncSession, *, instrument_id: uuid.UUID, timeframe: str
) -> datetime | None:
    """Most recent stored bucket start for the series (None when empty)."""
    ts = await session.scalar(
        select(CandleRow.ts)
        .where(CandleRow.instrument_id == instrument_id, CandleRow.timeframe == timeframe)
        .order_by(CandleRow.ts.desc())
        .limit(1)
    )
    return ts


async def first_closed_ts(
    session: AsyncSession, *, instrument_id: uuid.UUID, timeframe: str
) -> datetime | None:
    ts = await session.scalar(
        select(CandleRow.ts)
        .where(CandleRow.instrument_id == instrument_id, CandleRow.timeframe == timeframe)
        .order_by(CandleRow.ts.asc())
        .limit(1)
    )
    return ts


async def load_candles(
    session: AsyncSession,
    *,
    instrument_id: uuid.UUID,
    timeframe: str,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 5000,
) -> list[CandleRow]:
    """Stored bars ascending; optional half-open [start, end) window."""
    query = (
        select(CandleRow)
        .where(CandleRow.instrument_id == instrument_id, CandleRow.timeframe == timeframe)
        .order_by(CandleRow.ts.asc())
        .limit(limit)
    )
    if start is not None:
        query = query.where(CandleRow.ts >= start)
    if end is not None:
        query = query.where(CandleRow.ts < end)
    result = await session.execute(query)
    return list(result.scalars().all())
