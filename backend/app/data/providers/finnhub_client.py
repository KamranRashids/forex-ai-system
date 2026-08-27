"""Finnhub HTTP client shared by the news/calendar adapters (Phase 4).

Handles authentication (token), transient-error classification, retries and
rate-limiting — all the messy external I/O. The API token is used only to
authenticate outgoing requests; it is never stored in persistence, embedded in
``raw_payload``, logged, raised in exceptions, or returned by the API
(implementation requirement #7).
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.data.providers.content_base import (
    ContentProviderAuthError,
    ContentProviderRateLimitError,
    ContentProviderTransientError,
)

_BASE_URL: str = "https://finnhub.io/api/v1"


def _classify(
    status_code: int,
) -> ContentProviderAuthError | ContentProviderTransientError | ContentProviderRateLimitError:
    if status_code in (401, 403):
        # i18n-free, token-agnostic message (no credential material).
        return ContentProviderAuthError(f"Finnhub HTTP {status_code}: authentication failed")
    if status_code == 429:
        return ContentProviderRateLimitError("Finnhub HTTP 429: rate limit exceeded")
    if 500 <= status_code < 600:
        return ContentProviderTransientError(f"Finnhub HTTP {status_code}: upstream error")
    return ContentProviderTransientError(f"Finnhub HTTP {status_code}: unexpected response")


class FinnhubClient:
    """Minimal authenticated GET interface to Finnhub REST endpoints."""

    def __init__(self, api_token: str, *, http: httpx.AsyncClient | None = None) -> None:
        if not api_token:
            raise ContentProviderAuthError(
                "FINNHUB_API_TOKEN is empty; use NEWS_PROVIDER/CALENDAR_PROVIDER=synthetic "
                "to run without Finnhub credentials"
            )
        self._owns_client = http is None
        self._client = http or httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=httpx.Timeout(30.0),
        )
        self._token = api_token

    @retry(
        retry=retry_if_exception_type(ContentProviderTransientError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.2, max=2),
        reraise=True,
    )
    async def get_json(self, path: str, params: dict[str, str]) -> Any:
        """GET ``path`` with query params + token; returns parsed JSON (any shape)."""
        query = dict(params)
        query["token"] = self._token
        try:
            response = await self._client.get(path, params=query)
        except httpx.TransportError as exc:
            raise ContentProviderTransientError(
                f"Finnhub network error: {exc.__class__.__name__}"
            ) from exc
        if response.is_error:
            raise _classify(response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise ContentProviderTransientError("Finnhub returned non-JSON body") from exc

    async def get_json_list(self, path: str, params: dict[str, str]) -> list[Any]:
        """GET a path expected to return a JSON array (tolerates dict wrapper)."""
        data = await self.get_json(path, params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return []  # provider sometimes wraps arrays; treat as empty
        raise ContentProviderTransientError(f"Finnhub unexpected body shape for {path}")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
