"""Deterministic idempotency keys for news/calendar persistence (Phase 4).

Requirement: dedup keys are computed only from *stable, provider-non*changing*
fields (headline, url, canonical time, provider, external id). Ingestion time
and other changing fields are never included, so a redelivered/repolled record
maps to the same key and cannot duplicate.
"""

from __future__ import annotations

import hashlib

from app.data.content_types import NormalizedEconomicEvent, NormalizedNewsItem


def _hash_canonical(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest


def calendar_dedup_key(event: NormalizedEconomicEvent) -> str:
    """Stable dedup key for a calendar event.

    Prefer the provider's external id when available; otherwise fall back to a
    hash of the stable identifying fields so run-to-run the same event maps to
    the same key.
    """
    if event.external_id:
        return _hash_canonical("ext", event.provider.lower(), event.external_id)
    return _hash_canonical(
        "fields",
        event.provider.lower(),
        event.title.strip().lower(),
        event.timestamp_utc.isoformat(),
        event.currency.upper(),
    )


def news_item_hash(item: NormalizedNewsItem) -> str:
    """Stable, provider-independent dedup hash for a news item.

    Uses headline + url + published time + provider/external id — never
    ingestion time. Provider is included so two genuinely different provider
    records cannot collide.
    """
    headline = item.headline.strip().lower()
    url = (item.url or "").strip().lower()
    ts = item.published_utc.isoformat()
    return _hash_canonical(
        "news", item.provider.lower(), (item.external_id or ""), url, headline, ts
    )
