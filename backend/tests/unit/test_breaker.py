"""Unit tests for the provider circuit breaker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.data.breaker import BreakerOpenError, BreakerState, CircuitBreaker


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker(
        provider_name="synthetic", failure_threshold=3, cooldown_seconds=60, clock=clock
    )


@pytest.mark.unit
def test_opens_after_consecutive_failures(breaker: CircuitBreaker, clock: FakeClock) -> None:
    assert breaker.state is BreakerState.CLOSED
    assert breaker.record_failure() is False
    assert breaker.record_failure() is False
    opened = breaker.record_failure()
    assert opened is True
    assert breaker.state is BreakerState.OPEN
    assert breaker.opened_at == clock.now


@pytest.mark.unit
def test_success_resets_failure_count(breaker: CircuitBreaker) -> None:
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.consecutive_failures == 0
    assert breaker.record_failure() is False  # threshold restarts from zero


@pytest.mark.unit
def test_open_breaker_refuses_calls_until_cooldown(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    for _ in range(3):
        breaker.record_failure()
    with pytest.raises(BreakerOpenError, match="probe allowed in"):
        breaker.before_call()

    clock.advance(30)
    with pytest.raises(BreakerOpenError):
        breaker.before_call()

    clock.advance(31)  # total 61s > cooldown 60s
    breaker.before_call()  # transitions to HALF_OPEN without raising
    assert breaker.state is BreakerState.HALF_OPEN


@pytest.mark.unit
def test_half_open_probe_success_closes(breaker: CircuitBreaker, clock: FakeClock) -> None:
    for _ in range(3):
        breaker.record_failure()
    clock.advance(60)
    breaker.before_call()
    assert breaker.state is BreakerState.HALF_OPEN

    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.consecutive_failures == 0


@pytest.mark.unit
def test_half_open_probe_failure_reopens_immediately(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    for _ in range(3):
        breaker.record_failure()
    clock.advance(60)
    breaker.before_call()
    opened_again = breaker.record_failure()
    assert opened_again is True
    assert breaker.state is BreakerState.OPEN
    assert breaker.opened_at == clock.now


@pytest.mark.unit
def test_load_hydrates_persisted_state(clock: FakeClock) -> None:
    breaker = CircuitBreaker(provider_name="oanda", clock=clock)
    breaker.load(state="open", consecutive_failures=5, opened_at=clock.now - timedelta(seconds=90))
    # Cooldown already elapsed during downtime -> before_call half-opens.
    breaker.before_call()
    assert breaker.state is BreakerState.HALF_OPEN


@pytest.mark.unit
def test_load_invalid_state_falls_back_closed(clock: FakeClock) -> None:
    breaker = CircuitBreaker(provider_name="x", clock=clock)
    breaker.load(state="bogus", consecutive_failures=2, opened_at=None)
    assert breaker.state is BreakerState.CLOSED
