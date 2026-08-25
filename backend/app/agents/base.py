"""Core agent contract (IMPLEMENTATION_PLAN §3.3).

Every agent implements :class:`BaseAgent`: a stable ``id``, a ``version``
bumped whenever its logic changes (persisted with every signal), and an
``analyze`` coroutine turning an :class:`AnalysisContext` into an
:class:`AgentSignal`.

SAFE MODE: agents produce analysis only. Nothing in this module can place,
route, or describe the execution of an order.
"""

from __future__ import annotations

import abc
import uuid
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.data.timeframes import Timeframe


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


#: Signal lifetime multipliers relative to the timeframe interval.
FRESHNESS_MULTIPLIER: dict[str, int] = {"technical": 2, "regime": 4}
_DEFAULT_FRESHNESS_MULTIPLIER: int = 2


class AnalysisContext:
    """Inputs handed to an agent for one closed bar.

    ``candles`` is a DataFrame indexed by bucket-start timestamps (ascending)
    with columns open/high/low/close/volume; the last row corresponds to
    ``bucket_ts``.
    """

    __slots__ = ("bucket_ts", "candles", "meta", "now", "symbol", "timeframe")

    def __init__(
        self,
        *,
        symbol: str,
        timeframe: str,
        bucket_ts: datetime,
        candles: pd.DataFrame,
        now: datetime,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.bucket_ts = bucket_ts
        self.candles = candles
        self.now = now
        self.meta = meta or {}


class AgentSignal(BaseModel):
    """One agent's structured output for one closed bar (audit-persisted)."""

    model_config = ConfigDict(validate_assignment=True)

    agent_id: str = Field(min_length=1, max_length=32)
    version: str = Field(min_length=1, max_length=16)
    symbol: str = Field(min_length=6, max_length=12)
    timeframe: str
    direction: Direction
    confidence: float = Field(ge=0.0, le=1.0)
    bucket_ts: datetime
    rationale: str = ""
    features: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    valid_until: datetime
    run_id: str = ""

    @field_validator("timeframe")
    @classmethod
    def _known_timeframe(cls, value: str) -> str:
        if not Timeframe.is_valid(value.upper()):
            raise ValueError(f"unsupported timeframe {value!r}")
        return value.upper()

    def is_fresh(self, now: datetime) -> bool:
        """A signal is fresh until its valid_until instant."""
        return now <= self.valid_until


def valid_until_for(*, agent_id: str, bucket_ts: datetime, timeframe: str) -> datetime:
    """Signal expiry policy per agent family and timeframe."""
    multiplier = FRESHNESS_MULTIPLIER.get(agent_id, _DEFAULT_FRESHNESS_MULTIPLIER)
    return bucket_ts + timedelta(seconds=Timeframe.seconds(timeframe) * multiplier)


def new_run_id() -> str:
    """Fresh correlation id for one processing batch."""
    return uuid.uuid4().hex


class BaseAgent(abc.ABC):
    """Contract every analysis agent implements (§3.3)."""

    id: str = ""
    version: str = "0"

    @abc.abstractmethod
    def analyze(self, ctx: AnalysisContext) -> AgentSignal:
        """Produce a signal for the closed bar described by ``ctx``.

        Implementations are synchronous by design: indicator math is pure CPU
        work on in-memory data. The method is deliberately *not* a coroutine —
        determinism and testability beat ceremony; the worker awaits nothing
        inside agents.
        """

    def build_signal(
        self,
        ctx: AnalysisContext,
        *,
        direction: Direction,
        confidence: float,
        rationale: str,
        features: dict[str, Any],
    ) -> AgentSignal:
        """Assemble a validated signal with the standard freshness policy."""
        run_id = str(ctx.meta.get("run_id", ""))
        return AgentSignal(
            agent_id=self.id,
            version=self.version,
            symbol=ctx.symbol,
            timeframe=ctx.timeframe,
            direction=direction,
            confidence=max(0.0, min(1.0, confidence)),
            bucket_ts=ctx.bucket_ts,
            rationale=rationale,
            features=features,
            created_at=ctx.now,
            valid_until=valid_until_for(
                agent_id=self.id, bucket_ts=ctx.bucket_ts, timeframe=ctx.timeframe
            ),
            run_id=run_id,
        )
