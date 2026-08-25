"""DataProvider protocol and shared market-data value types (ADR-0003).

Contract notes:
- ``fetch_candles`` returns only *closed* bars whose bucket start lies in
  ``[start, end)``; forming bars are never produced.
- Prices are :class:`decimal.Decimal` quantized to the instrument's precision;
  volumes are integers.
- Implementations must be safe to construct without network access; failures
  raise :class:`ProviderError` (transient subclasses enable breaker logic).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable


class ProviderError(Exception):
    """Base error for market-data provider failures."""


class ProviderTransientError(ProviderError):
    """Retryable failure (timeouts, 5xx, rate limits)."""


@dataclass(frozen=True, slots=True)
class Candle:
    """One closed OHLCV bar for ``(symbol, timeframe, bucket_start)``."""

    symbol: str
    timeframe: str
    bucket_start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    complete: bool = True

    def validate(self) -> None:
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(
                f"Invalid OHLC for {self.symbol} {self.timeframe}@{self.bucket_start}: "
                f"low={self.low} open={self.open} close={self.close} high={self.high}"
            )


@dataclass(frozen=True, slots=True)
class Tick:
    """One streaming quote (bid/ask) from a provider."""

    symbol: str
    bid: Decimal
    ask: Decimal
    ts: datetime
    source: str = ""


@runtime_checkable
class DataProvider(Protocol):
    """Vendor-agnostic market data interface (ADR-0003)."""

    name: str

    async def fetch_candles(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        """Return closed candles with ``start <= bucket_start < end``."""
        ...

    async def aclose(self) -> None:
        """Release underlying resources."""
        ...


@runtime_checkable
class QuoteStreamProvider(Protocol):
    """Optional capability: live quote streaming (e.g. OANDA practice)."""

    def stream_quotes(self, symbols: list[str]) -> AsyncIterator[Tick]: ...
