"""System endpoints: root metadata, health probes, status matrix, metrics.

SAFE MODE (layer L4): the effective trading mode is surfaced by every health
endpoint so operators and the UI can assert the process is in paper-only mode.
``/health/ready`` additionally refuses "ready" unless the mode is safe.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DBSession, SettingsDep
from app.core.config import Settings
from app.core.constants import APP_NAME, APP_VERSION, SAFE_TRADING_MODE
from app.core.errors import ServiceUnavailableError
from app.core.security import utcnow
from app.db.session import get_redis
from app.schemas.common import ComponentStatus, SystemStatusOut

router = APIRouter(tags=["system"])

_ALEMBIC_INI: Path = Path(__file__).resolve().parents[3] / "alembic.ini"


@router.get("/")
async def root(settings: SettingsDep) -> dict[str, str]:
    """Service metadata."""
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "mode": settings.trading_mode,
        "docs": "/docs",
    }


@router.get("/health/live")
async def health_live(settings: SettingsDep) -> dict[str, str]:
    """Liveness probe — process is up and running in SAFE MODE."""
    return {"status": "ok", "mode": settings.trading_mode}


async def _check_db(session: AsyncSession) -> ComponentStatus:
    started = time.perf_counter()
    try:
        await session.execute(text("SELECT 1"))
        return ComponentStatus(ok=True, latency_ms=round((time.perf_counter() - started) * 1000, 2))
    except Exception as exc:  # noqa: BLE001 - component check reports any failure
        return ComponentStatus(
            ok=False, latency_ms=round((time.perf_counter() - started) * 1000, 2), detail=str(exc)
        )


async def _check_redis(redis: object) -> ComponentStatus:
    started = time.perf_counter()
    try:
        await asyncio.wait_for(redis.ping(), timeout=2.0)  # type: ignore[attr-defined]
        return ComponentStatus(ok=True, latency_ms=round((time.perf_counter() - started) * 1000, 2))
    except Exception as exc:  # noqa: BLE001
        return ComponentStatus(
            ok=False, latency_ms=round((time.perf_counter() - started) * 1000, 2), detail=str(exc)
        )


def _migration_heads() -> set[str]:
    """Read migration-head revisions from the alembic script directory."""
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(AlembicConfig(str(_ALEMBIC_INI)))
    return set(script.get_heads())


async def migrations_component(session: AsyncSession) -> ComponentStatus:
    """True when the applied alembic revision matches a migration head."""
    try:
        heads = _migration_heads()
    except Exception as exc:  # noqa: BLE001 - missing ini in odd deployments
        return ComponentStatus(ok=False, detail=f"alembic config unavailable: {exc}")

    try:
        result = await session.execute(text("SELECT version_num FROM alembic_version"))
        current = result.scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        return ComponentStatus(ok=False, detail=f"alembic_version unreadable: {exc}")

    ok = current is not None and current in heads
    return ComponentStatus(
        ok=ok,
        detail=None if ok else f"applied={current!r} heads={sorted(heads)!r}",
    )


async def collect_status(
    session: AsyncSession,
    redis: object,
    settings: Settings,
) -> dict[str, ComponentStatus]:
    db_status = await _check_db(session)
    redis_status = await _check_redis(redis)
    migrations = await migrations_component(session)
    safe_mode = settings.trading_mode == SAFE_TRADING_MODE
    components = {
        "database": db_status,
        "redis": redis_status,
        "migrations": migrations,
        "safe_mode": ComponentStatus(ok=safe_mode, detail=settings.trading_mode),
    }
    return components


@router.get("/health/ready")
async def health_ready(
    session: DBSession,
    redis: Annotated[object, Depends(get_redis)],
    settings: SettingsDep,
) -> dict[str, str]:
    """Readiness: DB + Redis reachable, migrations current, SAFE MODE asserted."""
    components = await collect_status(session, redis, settings)
    failed = {name: comp for name, comp in components.items() if not comp.ok}
    if failed:
        raise ServiceUnavailableError(
            "Readiness checks failed",
            extras={"failed_components": list(failed.keys())},
        )
    return {"status": "ok", "mode": settings.trading_mode}


@router.get("/system/status")
async def system_status(
    session: DBSession,
    redis: Annotated[object, Depends(get_redis)],
    settings: SettingsDep,
) -> SystemStatusOut:
    """Component matrix for dashboards; informational (always HTTP 200)."""
    components = await collect_status(session, redis, settings)
    return SystemStatusOut(
        name=APP_NAME,
        version=APP_VERSION,
        app_env=settings.app_env,
        trading_mode=settings.trading_mode,
        safe_mode=components["safe_mode"].ok,
        time_utc=utcnow(),
        components=components,
    )


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus scrape endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
