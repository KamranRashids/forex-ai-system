"""ASGI entrypoint for the API (SAFE MODE asserted at startup — layer L4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import FastAPI

from app.api.v1.admin import router as admin_router
from app.api.v1.auth import router as auth_router
from app.api.v1.content import router as content_router
from app.api.v1.decisions import router as decisions_router
from app.api.v1.risk import router as risk_router
from app.api.v1.signals import router as signals_router
from app.api.v1.system import router as system_router
from app.api.v1.users import router as users_router
from app.core.config import Settings, get_settings
from app.core.constants import API_V1_PREFIX, APP_VERSION
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.metrics import MetricsMiddleware
from app.db.session import dispose_engine, dispose_redis


def _log_banner(logger: Any, settings: Settings) -> None:
    logger.warning(
        "SAFE MODE ACTIVE: paper trading only. Live order execution is not implemented anywhere.",
        mode=settings.trading_mode,
        app_env=settings.app_env,
        version=APP_VERSION,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # SAFE MODE banner is logged in create_app() so it fires on every import
    # (including reloads and test clients); here we only manage resources.
    yield
    await dispose_engine()
    await dispose_redis()


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    configure_logging(log_level=resolved.log_level, json_logs=resolved.json_logs)
    logger = _get_logger()

    app = FastAPI(
        title="Forex AI System API",
        version=APP_VERSION,
        description=(
            "Multi-agent Forex analysis API.\n\n"
            "**SAFE MODE: PAPER TRADING ONLY.** This system never connects to a "
            "brokerage and cannot place live orders; live order execution does "
            "not exist in this codebase."
        ),
        license_info={"name": "MIT"},
        openapi_tags=[
            {"name": "system", "description": "Service metadata, health probes, status matrix"},
            {"name": "auth", "description": "Registration, login, token rotation, logout"},
            {"name": "users", "description": "Admin user management (RBAC-gated)"},
            {
                "name": "admin",
                "description": "Market universe config and backfill triggers (admin-only)",
            },
            {"name": "signals", "description": "Persisted agent signals (viewer+)"},
            {
                "name": "content",
                "description": "Normalized news + economic calendar (viewer+)",
            },
            {
                "name": "decisions",
                "description": "Orchestrator decisions + risk evaluations (viewer+)",
            },
            {
                "name": "risk",
                "description": "Risk state + tunable params (state read admin; params admin)",
            },
        ],
        lifespan=lifespan,
    )

    origins = resolved.cors_origin_list
    if origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
        )

    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(MetricsMiddleware)

    register_exception_handlers(app)

    # System/health endpoints live at the root (probe-friendly).
    app.include_router(system_router)
    app.include_router(auth_router, prefix=API_V1_PREFIX)
    app.include_router(users_router, prefix=API_V1_PREFIX)
    app.include_router(admin_router, prefix=API_V1_PREFIX)
    app.include_router(signals_router, prefix=API_V1_PREFIX)
    app.include_router(content_router, prefix=API_V1_PREFIX)
    app.include_router(decisions_router, prefix=API_V1_PREFIX)
    app.include_router(risk_router, prefix=API_V1_PREFIX)

    _log_banner(logger, resolved)
    return app


def _get_logger() -> Any:
    from structlog.stdlib import get_logger

    return get_logger(__name__)


app = create_app()
