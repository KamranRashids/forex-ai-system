"""OpenCode Zen / Ox Alpha Free LLM adapter (ADR-0004 initial provider).

External I/O confined here; the token is used only to authenticate outgoing
requests and is never persisted or logged. If unkeyed, the client reports
disabled and every call raises :class:`LLMUnavailable`, so agents fall back to
their deterministic path.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime

import httpx

from app.bus.events import Event
from app.bus.publisher import EventPublisher, NullEventPublisher
from app.core.config import get_settings
from app.llm.client import DailyBudgetBreaker, LLMUnavailable


class OpenCodeZenClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        budget_usd: float,
        max_tokens: int,
        timeout_seconds: float,
        http: httpx.AsyncClient | None = None,
        alert_publisher: EventPublisher | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._max_tokens = max(16, int(max_tokens))
        self._timeout = timeout_seconds
        self._budget = DailyBudgetBreaker(budget_usd)
        self._owns_client = http is None
        self._client = http or httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        self._alert_publisher = alert_publisher or NullEventPublisher()
        self._budget_alerted = False

    @classmethod
    def from_settings(cls) -> OpenCodeZenClient:
        s = get_settings()
        return cls(
            api_key=s.opencode_zen_api_key,
            model=s.opencode_zen_model,
            base_url=s.opencode_zen_base_url,
            budget_usd=s.llm_daily_budget_usd,
            max_tokens=s.llm_max_tokens,
            timeout_seconds=s.llm_timeout_seconds,
        )

    @property
    def enabled(self) -> bool:
        return bool(self._api_key) and self._budget.remaining > 0

    async def complete(self, system: str, prompt: str, *, max_tokens: int = 256) -> str:
        if not self._api_key:
            raise LLMUnavailable("OPENCODE_ZEN_API_KEY is empty")
        tokens = min(max(16, int(max_tokens)), self._max_tokens)
        try:
            self._budget.authorize(tokens)
        except LLMUnavailable:
            await self._emit_budget_alert()
            raise
        try:
            response = await self._client.post(
                f"{self._base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": tokens,
                },
            )
        except httpx.TransportError as exc:
            raise LLMUnavailable(f"LLM transport error: {exc.__class__.__name__}") from exc
        if response.is_error:
            raise LLMUnavailable(f"LLM HTTP {response.status_code}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable("LLM returned unexpected body") from exc
        if not isinstance(content, str):
            raise LLMUnavailable("LLM returned non-string content")
        return content.strip()

    async def _emit_budget_alert(self) -> None:
        """Publish a durable ``alert.llm_budget`` once when the daily budget trips."""
        if self._budget_alerted:
            return
        self._budget_alerted = True
        event = Event(
            event_type="alert.llm_budget",
            payload={
                "source": "llm",
                "severity": "warning",
                "state": "budget_exhausted",
            },
            producer="llm",
            produced_at=datetime.now(UTC),
        )
        with suppress(Exception):  # noqa: BLE001 - alerting must never break a call
            await self._alert_publisher.publish_alert(event)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
