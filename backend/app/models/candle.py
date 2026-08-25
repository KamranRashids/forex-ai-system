"""OHLCV candle store keyed by (instrument, timeframe, bucket start).

Bars are written via upsert-on-close semantics: a bar may be refreshed while
forming is *not* stored at all (providers only emit closed bars); once stored
with ``complete=True`` it is never downgraded.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Numeric, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

PRICE_PRECISION: int = 18
PRICE_SCALE: int = 8


class CandleRow(Base):
    __tablename__ = "candles"

    instrument_id: Mapped[str] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    timeframe: Mapped[str] = mapped_column(String(4), primary_key=True)
    #: Bucket start time (UTC); the bar covers [ts, ts + timeframe).
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(PRICE_PRECISION, PRICE_SCALE), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(PRICE_PRECISION, PRICE_SCALE), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(PRICE_PRECISION, PRICE_SCALE), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(PRICE_PRECISION, PRICE_SCALE), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Provider that produced the bar (audit trail across provider switches).
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Timeframe duration in minutes, denormalized for cheap filtering/sorting.
    tf_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
