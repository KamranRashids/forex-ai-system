"""Graceful shutdown coordination for runtime workers (Phase 7).

Each worker's ``run_*`` entrypoint installs a :class:`ShutdownCoordinator`
which captures SIGINT/SIGTERM into an ``asyncio.Event``. Long-running loops
check :meth:`ShutdownCoordinator.should_stop` each iteration and then run their
owner's cleanup (heartbeat clear, orchestrator lock release, provider close)
before returning. This yields clean, ordered shutdown for Docker ``SIGTERM``
and an orderly ``down`` heartbeat rather than a hard kill.

The coordinator is observation/control only and never touches trading.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress


class ShutdownCoordinator:
    """Collects OS shutdown signals into an asyncio.Event."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_running_loop()
        self._event = asyncio.Event()
        self._handlers: list[tuple[int, object]] = []
        self._install()

    def _install(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                handler = self._loop.add_signal_handler(sig, self._event.set)
            except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover
                # add_signal_handler is unavailable on some platforms (Windows);
                # rely on KeyboardInterrupt handling in worker_main instead.
                continue
            self._handlers.append((sig, handler))

    @property
    def event(self) -> asyncio.Event:
        return self._event

    @property
    def should_stop(self) -> bool:
        return self._event.is_set()

    async def wait(self, timeout: float | None = None) -> None:
        """Wait until a shutdown signal arrives, up to ``timeout`` seconds."""
        if timeout is None:
            await self._event.wait()
            return
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._event.wait(), timeout=timeout)

    def close(self) -> None:
        for sig, _handler in self._handlers:
            with suppress(RuntimeError, ValueError):
                self._loop.remove_signal_handler(sig)
        self._handlers.clear()
