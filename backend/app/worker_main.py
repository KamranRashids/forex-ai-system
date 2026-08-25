"""Worker process entrypoint; role selected via WORKER_ROLE.

Roles:
- ``ingest``      Phase 2 — market data pipeline (workers/ingest_worker.py)
- ``agents``      Phase 3 — technical/regime analysis (workers/agent_runtime.py)
- ``orchestrator`` | ``executor`` — later phases; refuse to start
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import Any

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging


def _logger() -> Any:
    return structlog.stdlib.get_logger("worker")


async def run_unimplemented_role(role: str) -> None:
    _logger().error(
        "worker_role_not_available_yet",
        role=role,
        arrives_in="Phase 5+ (see IMPLEMENTATION_PLAN.md section 14)",
    )


def main() -> None:
    settings = get_settings()
    configure_logging(log_level=settings.log_level, json_logs=settings.json_logs)
    role = os.getenv("WORKER_ROLE", "ingest").strip().lower()

    if role == "ingest":
        from app.workers.ingest_worker import run_ingest_worker

        coroutine = run_ingest_worker(settings)
    elif role == "agents":
        from app.workers.agent_runtime import run_agents_worker

        coroutine = run_agents_worker(settings)
    else:
        _logger().error("worker_role_not_available_yet", role=role, arrives_in="Phase 5+")
        raise SystemExit(2)

    with suppress(KeyboardInterrupt):  # pragma: no cover - operator interrupt
        asyncio.run(coroutine)


if __name__ == "__main__":
    main()
