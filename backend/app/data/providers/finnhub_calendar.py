"""Finnhub Economic Calendar adapter (Phase 4).

Normalizes the Finnhub calendar payload into :class:`NormalizedEconomicEvent`
boundary objects. Only the stable identifying fields are preserved into
``raw_payload``; the Finnhub token never appears in any persisted object.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.data.content_types import IMPORTANCE_LEVELS, NormalizedEconomicEvent
from app.data.providers.finnhub_client import FinnhubClient

#: Finnhub importance is not tagged; map by heuristics below, defaulting to
#: "medium" when unclear. Currencies come capitalized ISO (EUR, USD, ...).
_DEFAULT_IMPORTANCE: str = "medium"


def _parse_iso(raw: str) -> datetime:
    # Finnhub sends naive RFC3339-ish like "2026-08-27 08:00:00" -> assume UTC.
    cleaned = raw.strip().replace("Z", "")
    if "T" in cleaned:
        value = datetime.fromisoformat(cleaned)
    else:
        value = datetime.fromisoformat(cleaned.replace(" ", "T"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone()


def _classify_importance(title: str, event_payload: dict[str, Any]) -> str:
    title_l = title.lower()
    high_tokens = ("cpi", "gdp", "rate decision", "unemployment", "employment", "non-farm", "fed")
    low_tokens = (
        "trade balance",
        "minutes",
        "building permits",
        "michigan sentiment",
        "api weekly",
    )
    if any(tok in title_l for tok in high_tokens):
        return "high"
    if any(tok in title_l for tok in low_tokens):
        return "low"
    _ = event_payload
    return _DEFAULT_IMPORTANCE


def _sanitize_payload(item: dict[str, Any]) -> dict[str, Any]:
    # Copy only safe, non-credential fields; never the token or auth headers.
    keys = ("id", "country", "unit", "event", "currency", "date")
    return {k: item.get(k) for k in keys if k in item}


def normalize_event(item: object) -> NormalizedEconomicEvent | None:
    """Map one Finnhub calendar dict to a normalized event (or ``None``)."""
    if not isinstance(item, dict):
        return None
    title = (item.get("event") or "").strip()
    raw_date = item.get("date")
    if not title or not raw_date:
        return None
    try:
        ts = _parse_iso(str(raw_date))
    except ValueError:
        return None
    country = (item.get("country") or "").strip().upper()
    currency = (item.get("currency") or country or "").upper()
    if len(currency) != 3 or not currency.isalpha():
        currency = ""
    importance = _classify_importance(title, item)
    if importance not in IMPORTANCE_LEVELS:
        importance = _DEFAULT_IMPORTANCE
    external_id = item.get("id")
    external_id = str(external_id) if external_id is not None else None
    return NormalizedEconomicEvent(
        provider="finnhub",
        external_id=external_id,
        title=title,
        timestamp_utc=ts,
        importance=importance,
        currency=currency,
        actual=_opt_str(item.get("actual")),
        forecast=_opt_str(item.get("forecast")),
        previous=_opt_str(item.get("prev")),
        surprise_score=None,
        raw_payload=_sanitize_payload(item),
    )


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class FinnhubCalendarProvider:
    """CalendarProvider backed by Finnhub Economic Calendar."""

    name = "finnhub"

    def __init__(self, api_token: str, *, client: FinnhubClient | None = None) -> None:
        self._client = client or FinnhubClient(api_token)

    @classmethod
    def from_settings(cls, settings: object) -> FinnhubCalendarProvider:
        token = getattr(settings, "finnhub_api_token", "")
        return cls(api_token=token)

    async def fetch_events(
        self,
        *,
        since: datetime,
        until: datetime,
        symbols: Sequence[str] | None = None,
    ) -> list[NormalizedEconomicEvent]:
        params = {
            "from": since.date().isoformat(),
            "to": until.date().isoformat(),
        }
        data = await self._client.get_json("/calendar/economic", params=params)
        items = data.get("economicCalendar", []) if isinstance(data, dict) else []
        events = []
        for item in items:
            event = normalize_event(item)
            if event is None:
                continue
            if not (since <= event.timestamp_utc < until):
                continue
            events.append(event)
        return events

    async def aclose(self) -> None:
        await self._client.aclose()
