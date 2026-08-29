"""Alert event response models (Phase 8)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: str
    event_type: str
    source: str
    severity: str
    title: str
    message: str | None
    symbol: str | None
    timeframe: str | None
    producer: str
    correlation_id: str | None
    occurred_at: datetime
    payload: dict[str, Any]
    acknowledged_at: datetime | None
    acknowledged_by: str | None
    created_at: datetime


class AlertListOut(BaseModel):
    items: list[AlertOut]
    total: int
    limit: int
    offset: int


class AlertAckOut(BaseModel):
    event_id: str
    acknowledged_at: datetime
    acknowledged_by: str
