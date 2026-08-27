"""Deterministic seeded/demo news + economic-calendar content (Phase 4).

This is the default provider (zero external keys needed). Like the market-data
synthetic generator, output is a pure function of the query window + a fixed
seed: the same ``(since, until)`` always yields the same items, order- and
restart-safe. It is a *fixture* for development/tests/demos — never real news
or an actual economic calendar.
"""

from __future__ import annotations

import hashlib
import struct
from datetime import datetime, timedelta

from app.data.content_types import NormalizedEconomicEvent, NormalizedNewsItem

#: Major-currencies -> the FX pairs mentioning them (subset of the universe).
_CURRENCY_PAIRS: dict[str, tuple[str, ...]] = {
    "EUR": ("EURUSD",),
    "GBP": ("GBPUSD",),
    "USD": ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"),
    "JPY": ("USDJPY",),
    "AUD": ("AUDUSD",),
    "CAD": ("USDCAD",),
    "CHF": ("USDCHF",),
    "NZD": ("NZDUSD",),
}

#: (slug, importance) pairs used to synthesize event titles.
_CALENDAR_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("CPI YoY", "high"),
    ("Core CPI MoM", "medium"),
    ("GDP QoQ", "high"),
    ("Interest Rate Decision", "high"),
    ("Unemployment Rate", "high"),
    ("Non-Farm Employment Change", "high"),
    ("PMI Composite", "medium"),
    ("Retail Sales MoM", "medium"),
    ("Trade Balance", "low"),
    ("CB Policy Meeting Minutes", "medium"),
)


def _hash_uniform(*parts: str) -> float:
    key = "|".join(("forex-ai-synthetic-content",) + parts)
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    (raw,) = struct.unpack(">Q", digest)
    return float(raw) / float(1 << 64)


def _utc(ts: datetime) -> datetime:
    return ts.astimezone()


def _pct(
    kind: str,
    currency: str,
    template_idx: int,
    slot: int,
    scale: float,
    offset: float,
) -> str:
    value = _hash_uniform(f"{kind}|{currency}|{template_idx}|{slot}") * scale - offset
    return f"{round(value, 2):.2f}%"


def synthetic_calendar_events(since: datetime, until: datetime) -> list[NormalizedEconomicEvent]:
    """Deterministic calendar events with ``since <= ts < until``.

    Event density is fixed per 6-hour slot; each event's fields derive from the
    slot/currency/template, so re-polling the same window reproduces the same
    rows and idempotent dedup holds.
    """
    if until <= since:
        return []
    epoch = _utc(datetime(2024, 1, 1, tzinfo=since.tzinfo))
    slot_hours = 6
    first_slot = int((_utc(since) - epoch).total_seconds() // (slot_hours * 3600))
    events: list[NormalizedEconomicEvent] = []
    for slot in range(first_slot, first_slot + 12):
        ts = epoch + timedelta(hours=slot * slot_hours)
        if ts >= until:
            break
        for currency in _CURRENCY_PAIRS:
            for template_idx, (slug, importance) in enumerate(_CALENDAR_TEMPLATES):
                r = _hash_uniform(f"{currency}|{template_idx}|{slot}")
                if r > 0.35:  # keep ~65% of slots occupied across currencies
                    continue
                actual = None if r < 0.3 else _pct("a", currency, template_idx, slot, 8, 4)
                event_ts = ts + timedelta(
                    hours=_hash_uniform(f"t|{currency}|{template_idx}|{slot}") * slot_hours
                )
                events.append(
                    NormalizedEconomicEvent(
                        provider="synthetic",
                        external_id=f"synthetic-{slot}-{currency}-{template_idx}",
                        title=f"{currency} {slug}",
                        timestamp_utc=_utc(event_ts),
                        importance=importance,
                        currency=currency,
                        symbols=_CURRENCY_PAIRS[currency],
                        actual=actual,
                        forecast=_pct("f", currency, template_idx, slot, 6, 3),
                        previous=_pct("p", currency, template_idx, slot, 6, 3),
                        surprise_score=None,
                        raw_payload={"kind": "synthetic", "slot_id": slot},
                    )
                )
    return events


def synthetic_news_items(since: datetime, until: datetime) -> list[NormalizedNewsItem]:
    """Deterministic news stories within the window.

    A handful of headlines per day per currency, synthesized from a template
    lexicon; headline + url + published time form the stable dedup key so a
    repoll of the same window yields identical hashes.
    """
    if until <= since:
        return []
    prefixes = (
        "Central bank signals cautious tone",
        "Sector data beats forecasts",
        "Inflation prints mixed",
        "Markets eye upcoming rate decision",
        "Reserve update rattles sentiment",
    )
    currencies = ("EUR", "GBP", "USD", "JPY", "AUD")
    epoch = _utc(datetime(2024, 1, 1, tzinfo=since.tzinfo))
    items: list[NormalizedNewsItem] = []
    day = _utc(since).replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = _utc(until) - timedelta(minutes=1)
    while day <= end_day:
        day_ord = int((day - epoch).days)
        for story_idx in range(3):
            slug = prefixes[story_idx % len(prefixes)]
            currency = currencies[story_idx % len(currencies)]
            published = day + timedelta(hours=8 + story_idx * 4)
            if not (since <= published < until):
                continue
            headline = f"{slug}: {currency}"
            url = f"https://demo.example/news/{day_ord}/{story_idx}"
            items.append(
                NormalizedNewsItem(
                    provider="synthetic",
                    external_id=f"synthetic-news-{day_ord}-{story_idx}",
                    url=url,
                    headline=headline,
                    summary=(
                        "Deterministic demo summary for "
                        f"{currency} (internal fixture, not real news)."
                    ),
                    symbols=_CURRENCY_PAIRS[currency],
                    published_utc=_utc(published),
                    raw_payload={"kind": "synthetic", "day_ord": day_ord},
                )
            )
        day += timedelta(days=1)
    return items
