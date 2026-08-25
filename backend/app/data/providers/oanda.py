"""OANDA v20 **practice** adapter: REST candle history + streaming quotes.

SAFE MODE (ADR-0003): only the practice environment is wired — there is no
live-host code path to enable. ``OANDA_ENV`` is validated at settings level;
this adapter additionally hard-codes practice hosts.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.data.providers.base import Candle, ProviderError, ProviderTransientError, Tick
from app.data.timeframes import align_to_bucket, is_bar_closed
from app.models.instrument import pip_size_for

_API_HOSTS: dict[str, str] = {
    # SAFE MODE: only practice exists here by construction.
    "practice": "https://api-fxpractice.oanda.com",
}
_STREAM_HOSTS: dict[str, str] = {"practice": "https://stream-fxpractice.oanda.com"}

_GRANULARITY: dict[str, str] = {"M5": "M5", "M15": "M15", "H1": "H1", "H4": "H4", "D1": "D"}

_MAX_CANDLES_PER_REQUEST: int = 5000


def to_oanda_symbol(symbol: str) -> str:
    """EURUSD -> EUR_USD."""
    return f"{symbol[:3]}_{symbol[3:]}"


def parse_oanda_time(raw: str) -> datetime:
    """OANDA timestamps carry nanoseconds (RFC3339); trim to microseconds."""
    base = raw.strip().replace("Z", "")
    if "." in base:
        head, frac = base.split(".", 1)
        micro = frac[:6].ljust(6, "0")
        return datetime.fromisoformat(f"{head}.{micro}+00:00")
    return datetime.fromisoformat(base + "+00:00")


def _classify_status(status_code: int) -> ProviderError:
    if status_code in (429,) or 500 <= status_code < 600:
        return ProviderTransientError(f"OANDA HTTP {status_code}")
    return ProviderError(f"OANDA HTTP {status_code}")


class OandaPracticeProvider:
    """REST candles + chunked-JSON price stream against the practice API."""

    name = "oanda"

    def __init__(
        self,
        api_token: str,
        *,
        http: httpx.AsyncClient | None = None,
        stream_http: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_token:
            raise ValueError(
                "OANDA_API_TOKEN is empty; set it or use MARKET_DATA_PROVIDER=synthetic"
            )
        self._auth = {"Authorization": f"Bearer {api_token}", "Accept-Datetime-Format": "RFC3339"}
        self._owns_client = http is None
        self._client = http or httpx.AsyncClient(
            base_url=_API_HOSTS["practice"],
            headers=self._auth,
            timeout=httpx.Timeout(30.0),
        )
        self._stream_client = stream_http or httpx.AsyncClient(
            base_url=_STREAM_HOSTS["practice"],
            headers=self._auth,
            timeout=httpx.Timeout(10.0, read=None),
        )

    @classmethod
    def from_settings(cls, settings: Any) -> OandaPracticeProvider:
        """Build from app Settings (avoids a circular import at module load)."""
        return cls(api_token=settings.oanda_api_token)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        await self._stream_client.aclose()

    # --- REST candles -----------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((ProviderTransientError, httpx.TransportError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.2, max=2),
        reraise=True,
    )
    async def _get_candles_page(
        self, *, oanda_symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> dict[str, Any]:
        params: dict[str, str] = {
            "granularity": _GRANULARITY[timeframe],
            "price": "M",
            "from": start.isoformat(),
            "to": end.isoformat(),
            "count": str(_MAX_CANDLES_PER_REQUEST),
        }
        try:
            response = await self._client.get(
                f"/v3/instruments/{oanda_symbol}/candles", params=params
            )
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(f"OANDA timeout: {exc}") from exc
        except httpx.TransportError as exc:
            raise ProviderTransientError(f"OANDA transport error: {exc}") from exc

        if response.status_code != 200:
            raise _classify_status(response.status_code)
        return response.json()  # type: ignore[no-any-return]

    async def fetch_candles(
        self, *, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Candle]:
        body = await self._get_candles_page(
            oanda_symbol=to_oanda_symbol(symbol), timeframe=timeframe, start=start, end=end
        )
        pip = pip_size_for(symbol)
        decimals = 3 if pip == Decimal("0.01") else 5
        quantum = Decimal(1).scaleb(-decimals)

        candles: list[Candle] = []
        for raw in body.get("candles", []):
            if not raw.get("complete", False):
                continue  # contract: closed bars only
            bucket = align_to_bucket(parse_oanda_time(raw["time"]), timeframe)
            if not is_bar_closed(bucket, end, timeframe):
                continue
            if not (start <= bucket < end):
                continue
            mid = raw.get("mid", {})
            candle = Candle(
                symbol=symbol,
                timeframe=timeframe,
                bucket_start=bucket,
                open=Decimal(mid["o"]).quantize(quantum),
                high=Decimal(mid["h"]).quantize(quantum),
                low=Decimal(mid["l"]).quantize(quantum),
                close=Decimal(mid["c"]).quantize(quantum),
                volume=int(raw.get("volume", 0)),
                complete=bool(raw.get("complete", False)),
            )
            candle.validate()
            candles.append(candle)
        return candles

    # --- streaming quotes ---------------------------------------------------------

    async def stream_quotes(self, symbols: list[str]) -> AsyncIterator[Tick]:
        """Yield ticks from the practice price stream; heartbeats are skipped.

        Malformed lines are skipped (poison-pill tolerance); transport errors
        surface as ProviderTransientError for supervisor-level reconnects.
        """
        instruments_param = ",".join(to_oanda_symbol(s) for s in symbols)
        try:
            async with self._stream_client.stream(
                "GET", "/v3/prices/stream", params={"instruments": instruments_param}
            ) as response:
                if response.status_code != 200:
                    raise _classify_status(response.status_code)
                async for line in response.aiter_lines():
                    tick = self._parse_stream_line(line)
                    if tick is not None:
                        yield tick
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(f"OANDA stream timeout: {exc}") from exc
        except httpx.TransportError as exc:
            raise ProviderTransientError(f"OANDA stream transport error: {exc}") from exc

    @staticmethod
    def _parse_stream_line(line: str) -> Tick | None:
        stripped = line.strip()
        if not stripped:
            return None
        msg_type, _, payload = stripped.partition(" ")
        if msg_type != "PRICE":
            return None  # HEARTBEAT and anything unknown are ignored
        try:
            data = json.loads(payload)
            instrument: str = data["instrument"]
            time_raw: str = data["time"]
            ts = parse_oanda_time(time_raw)
            bid = Decimal(str(data["bids"][0]["price"]))
            ask = Decimal(str(data["asks"][0]["price"]))
        except Exception:  # noqa: BLE001 - malformed line: skip, keep the stream alive
            return None
        return Tick(symbol=instrument.replace("_", ""), bid=bid, ask=ask, ts=ts, source="oanda")
