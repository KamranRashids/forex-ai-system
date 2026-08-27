"""Unit tests for the LLM daily budget breaker (ADR-0004, requirement #5)."""

from __future__ import annotations

import pytest
from app.llm.client import DailyBudgetBreaker, LLMUnavailable

pytestmark = [pytest.mark.unit]


class FakeClock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_zero_budget_is_always_unavailable() -> None:
    breaker = DailyBudgetBreaker(0.0)
    assert breaker.remaining == 0.0
    with pytest.raises(LLMUnavailable, match="USD is 0"):
        breaker.reserve(10)


def test_reserve_against_budget_and_rollover() -> None:
    clock = FakeClock(1_000_000.0)
    breaker = DailyBudgetBreaker(budget_usd=0.20, now=clock)
    # ~5us/token -> 100 tokens = 0.0005; well within budget.
    breaker.reserve(100)
    assert breaker.remaining == pytest.approx(0.20 - 100 * 0.000005)
    with pytest.raises(LLMUnavailable, match="budget reached"):
        breaker.reserve(100_000_000)


def test_rollover_resets_spend_on_new_day() -> None:
    clock = FakeClock(1_000_000.0)
    breaker = DailyBudgetBreaker(budget_usd=0.0005, now=clock)
    breaker.reserve(50)  # 50 * 5e-6 = 0.00025
    with pytest.raises(LLMUnavailable):
        breaker.reserve(100_000_000)
    # Advance to a new calendar day -> spend is reset.
    clock.advance(24 * 3600)
    breaker.reserve(50)
    assert breaker.remaining > 0


def test_authorize_is_idempotent_gate() -> None:
    clock = FakeClock(1_000_000.0)
    breaker = DailyBudgetBreaker(budget_usd=0.10, now=clock)
    breaker.authorize(10)
    breaker.authorize(10)
    assert breaker.remaining == pytest.approx(0.10 - 2 * 10 * 0.000005)


def test_remaining_never_decreases_on_failed_reserve() -> None:
    clock = FakeClock(1_000_000.0)
    breaker = DailyBudgetBreaker(budget_usd=0.000001, now=clock)
    with pytest.raises(LLMUnavailable):
        breaker.reserve(1000)
    # A failed reserve does not consume spend: remaining stays non-negative.
    assert breaker.remaining == pytest.approx(0.000001)
    assert breaker.remaining >= 0.0
