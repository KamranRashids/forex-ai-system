"""Agent signal response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class SignalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent_id: str
    version: str
    symbol: str
    timeframe: str
    direction: str
    confidence: float
    bucket_ts: datetime
    rationale: str
    features: dict[str, Any]
    created_at: datetime
    valid_until: datetime
    run_id: str
