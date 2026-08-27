"""LLMClient abstraction + provider routing + daily budget breaker (ADR-0004).

Agents depend on this interface only; no agent ever calls an LLM provider
directly. The initial configured provider is OpenCode Zen / Ox Alpha Free, but
swapping providers is a config/adapter change. When the provider is disabled,
unkeyed, over budget, or failing, callers use a deterministic fallback — the
system is fully functional with zero LLM keys.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from app.core.config import Settings, get_settings


class LLMUnavailable(RuntimeError):
    """Raised when an LLM call is not permitted (disabled/unkeyed/over budget)."""


class LLMClient(Protocol):
    """Minimal interface for prompt -> text synthesis.

    Implementations must be constructible without network access and must not
    raise on ordinary availability checks; failures surface as
    :class:`LLMUnavailable` (or provider exceptions) that callers translate to
    their deterministic fallback.
    """

    @property
    def enabled(self) -> bool:
        """Whether a call may be attempted (configured, keyed, in budget)."""
        ...

    async def complete(self, system: str, prompt: str, *, max_tokens: int = 256) -> str:
        """Return the synthesized text for the given prompt."""
        ...


class DailyBudgetBreaker:
    """Best-effort in-process daily USD spend ceiling.

    This is a lightweight per-process guard. It defers to the *configured*
    ``LLM_DAILY_BUDGET_USD``; a value of 0 disables LLM spend entirely (the
    zero-key default). Cost accounting uses the model's configured per-call
    estimate so no external pricing API is required.
    """

    _DOLLARS_PER_TOKEN: float = 0.000005  # placeholder conservative estimate.

    def __init__(self, budget_usd: float, *, now: Callable[[], float] | None = None) -> None:
        self._budget = max(0.0, budget_usd)
        self._day = 0
        self._spent = 0.0
        self._now = now or time.time

    def _rollover(self) -> None:
        today = int(self._now() // 86400)
        if today != self._day:
            self._day = today
            self._spent = 0.0

    @property
    def remaining(self) -> float:
        if self._budget <= 0:
            return 0.0
        self._rollover()
        return max(0.0, self._budget - self._spent)

    def reserve(self, tokens: int) -> None:
        """Reserve estimated cost in advance; raises LLMUnavailable if over budget."""
        if self._budget <= 0:
            raise LLMUnavailable("LLM_DAILY_BUDGET_USD is 0; no LLM spend permitted")
        self._rollover()
        cost = tokens * self._DOLLARS_PER_TOKEN
        if self._spent + cost > self._budget:
            raise LLMUnavailable("LLM daily budget reached")
        self._spent += cost

    def authorize(self, tokens: int) -> None:
        """Idempotent gate used by adapters before issuing a call."""
        self.reserve(tokens)


def build_llm_client(settings: Settings | None = None) -> LLMClient:
    """Construct the configured LLM client (falls back to a disabled client)."""
    resolved = settings or get_settings()
    provider = (resolved.llm_provider or "none").strip().lower()
    if provider == "opencode_zen":
        from app.llm.opencode_zen import OpenCodeZenClient

        return OpenCodeZenClient(
            api_key=resolved.opencode_zen_api_key,
            model=resolved.opencode_zen_model,
            base_url=resolved.opencode_zen_base_url,
            budget_usd=resolved.llm_daily_budget_usd,
            max_tokens=resolved.llm_max_tokens,
            timeout_seconds=resolved.llm_timeout_seconds,
        )
    return DisabledLLMClient()


class DisabledLLMClient:
    """No LLM configured/unkeyed; every call raises LLMUnavailable."""

    @property
    def enabled(self) -> bool:
        return False

    async def complete(self, system: str, prompt: str, *, max_tokens: int = 256) -> str:
        raise LLMUnavailable("no LLM provider configured or env is unkeyed")
