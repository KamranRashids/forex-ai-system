"""Backtest agent runner: rebuild signals exactly as the live pipeline would.

Reuses the *same* ``BaseAgent.analyze`` objects and the *same* candle-window
construction the live agent worker uses (see ``workers/agent_worker.py``), so
backtest signals for identical bars are identical to live (the "same signal
path" invariant).

Per decision D-B, historical **fundamental/sentiment** signals are replayed from
persisted ``agent_signals`` rows (never re-run), while **technical/regime** are
recomputed deterministically from candles because they depend only on price
history. Missing replay coverage is reported explicitly and never fabricated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from app.agents.base import AgentSignal, AnalysisContext
from app.agents.registry import default_registry
from app.data.providers.base import Candle

REPLAY_AGENTS: tuple[str, ...] = ("fundamental", "sentiment")
COMPUTE_AGENTS: tuple[str, ...] = ("technical", "regime")


@dataclass(slots=True)
class AgentRunResult:
    """Signals for one bar plus which agents were replayed vs computed."""

    signals: dict[str, AgentSignal] = field(default_factory=dict)
    computed: set[str] = field(default_factory=set)
    replayed: set[str] = field(default_factory=set)


class BacktestAgentRunner:
    """Runs the production agents over an in-memory candle window."""

    def __init__(self) -> None:
        self._registry = default_registry()
        #: persisted historical signals keyed by (agent_id, symbol, timeframe,
        #: bucket_ts) -> AgentSignal (replayed for fundamental/sentiment).
        self.replay: dict[tuple[str, str, str, datetime], AgentSignal] = {}

    def add_replayed(self, *signals: AgentSignal) -> None:
        for sig in signals:
            if sig.agent_id in REPLAY_AGENTS:
                self.replay[(sig.agent_id, sig.symbol.upper(), sig.timeframe, sig.bucket_ts)] = sig

    def analyze(
        self,
        *,
        symbol: str,
        timeframe: str,
        bucket_ts: datetime,
        candles: list[Candle],
        now: datetime,
        prev_daily: dict[str, float] | None = None,
        pip_size: float = 0.001,
    ) -> AgentRunResult:
        frame = self._frame(candles)
        run_id = f"bt|{symbol}|{timeframe}|{bucket_ts.isoformat()}"
        ctx = AnalysisContext(
            symbol=symbol,
            timeframe=timeframe,
            bucket_ts=bucket_ts,
            candles=frame,
            now=now,
            meta={"run_id": run_id, "prev_daily": prev_daily, "pip_size": pip_size},
        )
        result = AgentRunResult()
        for agent in self._registry.all():
            if agent.id in REPLAY_AGENTS:
                replayed = self.replay.get((agent.id, symbol.upper(), timeframe, bucket_ts))
                if replayed is not None:
                    result.signals[agent.id] = replayed
                    result.replayed.add(agent.id)
                continue
            result.signals[agent.id] = agent.analyze(ctx)
            result.computed.add(agent.id)
        return result

    @staticmethod
    def _frame(candles: list[Candle]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "open": [float(c.open) for c in candles],
                "high": [float(c.high) for c in candles],
                "low": [float(c.low) for c in candles],
                "close": [float(c.close) for c in candles],
                "volume": [int(c.volume) for c in candles],
            },
            index=[c.bucket_start for c in candles],
        )
