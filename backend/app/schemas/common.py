"""Health / system status models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ComponentStatus(BaseModel):
    ok: bool
    latency_ms: float | None = None
    detail: str | None = None


class WorkerHealthOut(BaseModel):
    status: str
    last_seen: datetime | None = None
    started_at: datetime | None = None
    age_seconds: float | None = None
    ttl_seconds: int


class SystemStatusOut(BaseModel):
    name: str
    version: str
    app_env: str
    trading_mode: str
    safe_mode: bool
    time_utc: datetime
    components: dict[str, ComponentStatus]
    workers: dict[str, WorkerHealthOut]
