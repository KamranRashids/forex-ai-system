"""Market data read models (Phase 12 O-1): candles + latest price quotes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class CandleOut(BaseModel):
    symbol: str
    timeframe: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class LatestPriceOut(BaseModel):
    symbol: str
    price: Decimal
    bucket_start: datetime | None
    synthetic: bool
