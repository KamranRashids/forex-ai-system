"""Unit tests for the sliding-window rate limiter."""

from __future__ import annotations

import pytest
from app.core.ratelimit import SlidingWindowLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.mark.unit
def test_allows_up_to_limit_then_blocks(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(clock=clock)
    for _ in range(3):
        assert limiter.check("k", limit=3).allowed
    result = limiter.check("k", limit=3)
    assert not result.allowed
    assert result.retry_after_seconds >= 1


@pytest.mark.unit
def test_window_slides_and_reallows(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(clock=clock)
    for _ in range(3):
        assert limiter.check("k", limit=3, window_seconds=60).allowed
    assert not limiter.check("k", limit=3, window_seconds=60).allowed
    clock.advance(61)
    assert limiter.check("k", limit=3, window_seconds=60).allowed


@pytest.mark.unit
def test_keys_are_independent(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(clock=clock)
    assert limiter.check("ip:a", limit=1).allowed
    assert not limiter.check("ip:a", limit=1).allowed
    assert limiter.check("ip:b", limit=1).allowed


@pytest.mark.unit
def test_retry_after_reflects_window_position(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(clock=clock)
    assert limiter.check("k", limit=1, window_seconds=60).allowed
    clock.advance(10)
    result = limiter.check("k", limit=1, window_seconds=60)
    assert not result.allowed
    # First hit expires at 1060; now is 1010 -> ~50s remaining.
    assert 45 <= result.retry_after_seconds <= 51


@pytest.mark.unit
def test_reset_clears_state(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(clock=clock)
    assert limiter.check("k", limit=1).allowed
    limiter.reset("k")
    assert limiter.check("k", limit=1).allowed
