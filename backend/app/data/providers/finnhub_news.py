"""Finnhub News adapter (Phase 4).

Normalizes the Finnhub ``/news?category=forex`` payload into
:class:`NormalizedNewsItem` boundary objects. Only stable identifying fields
are preserved into ``raw_payload``; the Finnhub token never appears in any
persisted object.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.data.content_types import NormalizedNewsItem
from app.data.providers.finnhub_client import FinnhubClient

_CATEGORY: str = "forex"


def _parse_epoch(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, tz=UTC)


def _sanitize_payload(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("category", "id", "image", "related", "source")
    return {k: item.get(k) for k in keys if k in item}


def normalize_item(item: object) -> NormalizedNewsItem | None:
    """Map one Finnhub news dict to a normalized item (or ``None``)."""
    if not isinstance(item, dict):
        return None
    headline = (item.get("headline") or "").strip()
    if not headline:
        return None
    published = _parse_epoch(item.get("datetime"))
    if published is None:
        return None
    # Finnhub "related" is a comma-separated list of stock symbols; map USD/EUR
    # style tokens to our FX universe loosely (empty when ambiguous).
    symbols = tuple(s.upper() for s in _related_symbols(item) if _looks_fx(s))
    external_id = item.get("id")
    external_id = str(external_id) if external_id is not None else None
    return NormalizedNewsItem(
        provider="finnhub",
        external_id=external_id,
        url=(item.get("url") or "").strip() or None,
        headline=headline,
        summary=(item.get("summary") or "").strip() or None,
        symbols=symbols,
        published_utc=published,
        raw_payload=_sanitize_payload(item),
    )


def _related_symbols(item: dict[str, Any]) -> list[str]:
    related = item.get("related") or ""
    return [part.strip() for part in str(related).split(",") if part.strip()]


def _looks_fx(symbol: str) -> bool:
    return len(symbol) == 6 and symbol.isalpha()


class FinnhubNewsProvider:
    """NewsProvider backed by Finnhub general news (forex category)."""

    name = "finnhub"

    def __init__(self, api_token: str, *, client: FinnhubClient | None = None) -> None:
        self._client = client or FinnhubClient(api_token)

    @classmethod
    def from_settings(cls, settings: object) -> FinnhubNewsProvider:
        token = getattr(settings, "finnhub_api_token", "")
        return cls(api_token=token)

    async def fetch_news(
        self,
        *,
        since: datetime,
        until: datetime,
        symbols: Sequence[str] | None = None,
    ) -> list[NormalizedNewsItem]:
        raw = await self._client.get_json_list("/news", params={"category": _CATEGORY})
        items = []
        for item in raw:
            normalized = normalize_item(item)
            if normalized is None:
                continue
            if not (since <= normalized.published_utc < until):
                continue
            items.append(normalized)
        return items

    async def aclose(self) -> None:
        await self._client.aclose()
