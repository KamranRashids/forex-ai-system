"""Unit tests: graceful-shutdown coordinator (Phase 7 runtime hardening)."""

from __future__ import annotations

import asyncio

import pytest
from app.core.shutdown import ShutdownCoordinator


@pytest.mark.unit
async def test_coordinator_should_stop_false_until_signalled() -> None:
    coordinator = ShutdownCoordinator()
    try:
        assert coordinator.should_stop is False
        coordinator.event.set()
        assert coordinator.should_stop is True
        await coordinator.wait()  # returns immediately once set
    finally:
        coordinator.close()


@pytest.mark.unit
async def test_wait_timeout_does_not_stop() -> None:
    coordinator = ShutdownCoordinator()
    try:
        start = asyncio.get_running_loop().time()
        await coordinator.wait(timeout=0.05)
        elapsed = asyncio.get_running_loop().time() - start
        assert coordinator.should_stop is False
        assert elapsed >= 0.04
    finally:
        coordinator.close()


@pytest.mark.unit
async def test_close_is_idempotent() -> None:
    coordinator = ShutdownCoordinator()
    coordinator.close()
    coordinator.close()  # no-op must not raise
