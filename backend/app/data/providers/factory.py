"""Provider construction from settings (ADR-0003)."""

from __future__ import annotations

from datetime import datetime

from app.core.config import Settings
from app.data.providers.base import Candle, DataProvider, ProviderError
from app.data.providers.synthetic import synthetic_candles


class SyntheticProvider:
    """Adapter exposing the deterministic generator as a DataProvider."""

    name = "synthetic"

    async def fetch_candles(
        self, *, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Candle]:
        return synthetic_candles(symbol, timeframe, start, end)

    async def aclose(self) -> None:
        return None


def build_provider(settings: Settings) -> DataProvider:
    """Instantiate the configured provider; misconfig fails fast (L4)."""
    provider_name = settings.market_data_provider
    if provider_name == "synthetic":
        return SyntheticProvider()
    if provider_name == "oanda":
        from app.data.providers.oanda import OandaPracticeProvider

        return OandaPracticeProvider.from_settings(settings)
    raise ProviderError(f"Unknown MARKET_DATA_PROVIDER {provider_name!r}")
