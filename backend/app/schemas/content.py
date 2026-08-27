"""News + economic-calendar response models (Phase 4, UI-ready payloads)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class NewsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider: str
    url: str | None
    headline: str
    summary: str | None
    symbols: list[str]
    published_utc: datetime
    created_at: datetime


class EconomicEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider: str
    title: str
    currency: str
    symbols: list[str]
    importance: str
    timestamp_utc: datetime
    actual: str | None
    forecast: str | None
    previous: str | None
    surprise_score: Decimal | None
    created_at: datetime
