"""Placeholder API entrypoint for the Phase 0 scaffold.

Phase 1 replaces this with the real application core (typed settings,
database, auth). SAFE MODE validation below is intentionally already
enforced at layer L1/L4: the process refuses to boot unless
TRADING_MODE == "safe".
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

APP_NAME = "forex-ai-api"
APP_VERSION = "0.0.1"

LOGGER = logging.getLogger(APP_NAME)

#: The only trading mode the system accepts in its current form (SAFE MODE, layer L1).
ALLOWED_TRADING_MODES: frozenset[str] = frozenset({"safe"})


def validate_safe_mode(raw_trading_mode: str | None) -> str:
    """Validate the configured trading mode and return the normalized value.

    Raises RuntimeError for anything except "safe" — this is SAFE MODE layer L1.
    There is no bypass: live trading does not exist in this codebase.
    """
    mode = (raw_trading_mode or "").strip().lower()
    if mode not in ALLOWED_TRADING_MODES:
        raise RuntimeError(
            f"Refusing to start: TRADING_MODE={raw_trading_mode!r} is not permitted. "
            f"Only {sorted(ALLOWED_TRADING_MODES)!r} is allowed; "
            "live order execution does not exist."
        )
    return mode


def create_app() -> FastAPI:
    trading_mode = validate_safe_mode(os.getenv("TRADING_MODE", "safe"))
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    LOGGER.warning(
        "SAFE MODE ACTIVE: paper trading only. Live order execution is not implemented anywhere.",
    )

    app = FastAPI(
        title="Forex AI System API",
        version=APP_VERSION,
        description="Multi-agent Forex analysis — PAPER TRADING ONLY (SAFE MODE).",
    )

    @app.get("/", tags=["system"])
    def root() -> dict[str, str]:
        """Service metadata."""
        return {"name": APP_NAME, "version": APP_VERSION, "mode": trading_mode}

    @app.get("/health/live", tags=["health"])
    def health_live() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok", "mode": trading_mode}

    return app


app = create_app()
