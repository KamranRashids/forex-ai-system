"""In-memory circuit breaker for provider calls (state persisted by callers).

Transitions:
- CLOSED: normal operation; failures increment a counter.
- OPEN: entered after ``failure_threshold`` consecutive failures; all calls
  are refused until the cooldown elapses.
- HALF_OPEN: cooldown elapsed; a single probe call decides — success closes
  the breaker, failure reopens it.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from datetime import datetime

from app.data.providers.base import ProviderError


class BreakerState(enum.StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    @classmethod
    def from_value(cls, value: str | None) -> BreakerState:
        try:
            return cls(value or "closed")
        except ValueError:
            return cls.CLOSED


class BreakerOpenError(ProviderError):
    """Raised when a call is attempted while the breaker is open."""


class CircuitBreaker:
    def __init__(
        self,
        *,
        provider_name: str,
        failure_threshold: int = 5,
        cooldown_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.state = BreakerState.CLOSED
        self.consecutive_failures = 0
        self.opened_at: datetime | None = None

    # --- introspection -------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self._clock()

    def seconds_until_probe(self) -> int:
        if self.state is not BreakerState.OPEN or self.opened_at is None:
            return 0
        elapsed = (self.now - self.opened_at).total_seconds()
        return max(0, int(self.cooldown_seconds - elapsed))

    # --- lifecycle -----------------------------------------------------------

    def before_call(self) -> None:
        """Gate a call; transitions OPEN -> HALF_OPEN when cooldown elapsed.

        Raises:
            BreakerOpenError: while the breaker refuses calls.
        """
        if self.state is BreakerState.OPEN and self.opened_at is not None:
            if self.seconds_until_probe() <= 0:
                self.state = BreakerState.HALF_OPEN
                return
            raise BreakerOpenError(
                f"provider {self.provider_name!r} breaker open; "
                f"probe allowed in {self.seconds_until_probe()}s"
            )
        if self.state is BreakerState.HALF_OPEN:
            # Only one in-flight probe at a time; sequential loops are fine.
            raise BreakerOpenError(f"provider {self.provider_name!r} probe already in flight")

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None
        self.state = BreakerState.CLOSED

    def record_failure(self) -> bool:
        """Record a failure; returns True when this failure opened the breaker."""
        self.consecutive_failures += 1
        if self.state is BreakerState.HALF_OPEN or (
            self.consecutive_failures >= self.failure_threshold
        ):
            self._open()
            return True
        return False

    def _open(self) -> None:
        self.state = BreakerState.OPEN
        self.opened_at = self.now

    # --- persistence bridge ----------------------------------------------------

    def load(
        self,
        *,
        state: str,
        consecutive_failures: int,
        opened_at: datetime | None,
    ) -> None:
        """Hydrate from persisted state (worker restart).

        An OPEN breaker whose cooldown already elapsed during downtime
        transitions to HALF_OPEN lazily via :meth:`before_call`.
        """
        self.state = BreakerState.from_value(state)
        self.consecutive_failures = consecutive_failures
        self.opened_at = opened_at
