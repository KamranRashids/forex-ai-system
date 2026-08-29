"""Worker process entrypoint; role selected via WORKER_ROLE.

Roles:
- ``ingest``      Phase 2 — market data pipeline (workers/ingest_worker.py)
- ``agents``      Phase 3/4 — technical/regime/fundamental/sentiment analysis
- ``content``     Phase 4 — news + economic-calendar ingestion (content_runtime.py)
- ``orchestrator`` Phase 5 — fuse signals + risk-gate into decisions/paper intents
- ``alerts``      Phase 8 — persist alerts.stream into alert_events (alert_runtime.py)
- ``executor``     later phase; refuse to start
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
    elif role == "content":
        from app.workers.content_runtime import run_content_worker

        coroutine = run_content_worker(settings)
    elif role == "orchestrator":
        from app.workers.orchestrator_runtime import run_orchestrator

        coroutine = run_orchestrator(settings)
    elif role == "alerts":
        from app.workers.alert_runtime import run_alerts_worker

        coroutine = run_alerts_worker(settings)
    else:
        _logger().error("worker_role_not_available_yet", role=role, arrives_in="Phase 5+")
        raise SystemExit(2)

    with suppress(KeyboardInterrupt):  # pragma: no cover - operator interrupt
        asyncio.run(coroutine)


if __name__ == "__main__":
    main()
