"""Admin endpoints: market universe config + backfill triggers (admin-only)."""

from __future__ import annotations

import json
from collections.abc import Awaitable
from datetime import datetime
from typing import Annotated, Any, cast

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.api.deps import AdminUser, DBSession, SettingsDep, client_ip
from app.core.constants import AuditActions
from app.core.errors import InvalidInputError
from app.data.market_config import (
    SYMBOLS_KEY,
    TIMEFRAMES_KEY,
    get_market_config,
    set_market_config,
)
from app.data.timeframes import Timeframe
from app.db.session import get_redis
from app.models.audit_log import AuditLog

router = APIRouter(prefix="/admin", tags=["admin"])

RedisDep = Annotated[Redis, Depends(get_redis)]

_BACKFILL_QUEUE: str = "jobs:backfill"
_MAX_BACKFILL_DAYS: int = 90


class MarketConfigIn(BaseModel):
    symbols: list[str] | None = Field(default=None, max_length=64)
    timeframes: list[str] | None = Field(default=None, max_length=8)


class MarketConfigOut(BaseModel):
    provider: str
    symbols: list[str]
    timeframes: list[str]
    supported_timeframes: list[str]


class BackfillRequest(BaseModel):
    symbols: list[str] | None = Field(default=None, max_length=64)
    timeframes: list[str] | None = Field(default=None, max_length=8)
    start: datetime
    end: datetime


class BackfillAccepted(BaseModel):
    detail: str
    queued_jobs: int
    start: datetime
    end: datetime


@router.get("/market-config")
async def read_market_config(
    _admin: AdminUser, session: DBSession, settings: SettingsDep
) -> MarketConfigOut:
    """Effective symbol/timeframe universe (DB overrides beat env defaults)."""
    symbols, timeframes = await get_market_config(session, settings)
    return MarketConfigOut(
        provider=settings.market_data_provider,
        symbols=symbols,
        timeframes=timeframes,
        supported_timeframes=list(Timeframe.values()),
    )


@router.put("/market-config")
async def update_market_config(
    body: MarketConfigIn,
    session: DBSession,
    settings: SettingsDep,
    admin: AdminUser,
    request: Request,
) -> MarketConfigOut:
    """Override the runtime universe; audited. Omitted fields stay unchanged."""
    try:
        new_symbols, new_timeframes = await set_market_config(
            session, actor=admin.email, symbols=body.symbols, timeframes=body.timeframes
        )
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc

    session.add(
        AuditLog(
            actor=admin.email,
            action=AuditActions.MARKET_CONFIG_UPDATED,
            entity_type="system_setting",
            entity_id=f"{SYMBOLS_KEY}+{TIMEFRAMES_KEY}",
            after={"symbols": new_symbols, "timeframes": new_timeframes},
            ip_address=client_ip(request),
        )
    )
    return MarketConfigOut(
        provider=settings.market_data_provider,
        symbols=new_symbols,
        timeframes=new_timeframes,
        supported_timeframes=list(Timeframe.values()),
    )


@router.post("/backfill")
async def trigger_backfill(
    body: BackfillRequest,
    request: Request,
    session: DBSession,
    settings: SettingsDep,
    admin: AdminUser,
    redis: RedisDep,
) -> BackfillAccepted:
    """Queue a historical backfill job; the ingest worker drains it."""
    if body.end <= body.start:
        raise InvalidInputError("backfill end must be after start")
    if (body.end - body.start).days > _MAX_BACKFILL_DAYS:
        raise InvalidInputError(f"backfill range limited to {_MAX_BACKFILL_DAYS} days")

    effective_symbols, effective_timeframes = await get_market_config(session, settings)
    job = {
        "symbols": body.symbols or effective_symbols,
        "timeframes": body.timeframes or effective_timeframes,
        "start": body.start.isoformat(),
        "end": body.end.isoformat(),
    }
    await cast("Awaitable[Any]", redis.lpush(_BACKFILL_QUEUE, json.dumps(job)))
    session.add(
        AuditLog(
            actor=admin.email,
            action=AuditActions.BACKFILL_TRIGGERED,
            entity_type="backfill_job",
            entity_id=f"{job['symbols']}|{job['timeframes']}",
            after={"start": job["start"], "end": job["end"]},
            ip_address=client_ip(request),
        )
    )
    return BackfillAccepted(
        detail="Backfill queued; the ingest worker will drain it.",
        queued_jobs=1,
        start=body.start,
        end=body.end,
    )


async def drain_backfill_queue(redis: Any, limit: int = 5) -> list[dict[str, Any]]:
    """Pop queued backfill jobs (worker side); exposed for reuse/tests."""
    log = structlog.stdlib.get_logger(__name__)
    jobs: list[dict[str, Any]] = []
    while len(jobs) < limit:
        raw = await redis.rpop(_BACKFILL_QUEUE)
        if raw is None:
            break
        try:
            jobs.append(json.loads(raw))
        except json.JSONDecodeError:
            log.warning("backfill_job_unparseable")
    return jobs
