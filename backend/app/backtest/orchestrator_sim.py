"""Backtest orchestrator simulation: same decision path, broker-fed gates.

Reuses the production pure decision path (``compute_decision``) so backtest and
live produce identical decisions for identical inputs. The only difference from
the live ``DecisionEngine`` is where the risk-gate inputs come from: the
backtest feeds a ``GateState`` computed from the simulated broker's open
positions (the exposure source of truth) plus driver-tracked daily-loss and
drawdown, instead of reading DB risk state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.agents.base import AgentSignal, Direction
from app.backtest.models import BacktestConfig
from app.data.risk_config import RiskParams
from app.data.timeframes import Timeframe
from app.decisions.engine import (
    DecisionAction,
    DecisionInputs,
    DecisionOutcome,
    OrchParams,
    compute_decision,
)
from app.decisions.fusion import ContextInput
from app.decisions.risk import GateState

AGENT_FAMILIES: tuple[str, ...] = ("technical", "regime", "fundamental", "sentiment")


@dataclass(frozen=True, slots=True)
class RecentDecision:
    direction: Direction
    confidence: float
    valid_until: datetime
    bucket_ts: datetime


class BacktestDecisionEngine:
    """Produces decisions during a backtest without any DB writes."""

    def __init__(self, cfg: BacktestConfig) -> None:
        self.cfg = cfg
        orch = OrchParams(cooldown_seconds=0)
        self.orch = orch
        #: per (symbol, tf) -> RecentDecision for parent-context / cooldown.
        self._last: dict[tuple[str, str], RecentDecision] = {}

    def decide(
        self,
        *,
        symbol: str,
        timeframe: str,
        bucket_ts: datetime,
        now: datetime,
        signals: dict[str, AgentSignal],
        price: float,
        configured: list[str],
        gate: GateState,
        risk: RiskParams,
    ) -> DecisionOutcome:
        regime_sig = signals.get("regime")
        regime = (
            str((regime_sig.features or {}).get("regime", "unknown"))
            if regime_sig is not None
            else "unknown"
        )
        atr = _atr_of(signals.get("technical"))
        coverage = sum(1 for a in AGENT_FAMILIES if a in signals and signals[a] is not None) / len(
            AGENT_FAMILIES
        )
        parent = self._parent_context(
            symbol=symbol, timeframe=timeframe, configured=configured, now=now
        )

        inputs = DecisionInputs(
            symbol=symbol,
            timeframe=timeframe,
            bucket_ts=bucket_ts,
            now=now,
            signals=signals,
            regime=regime,
            atr=atr,
            price=price,
            coverage=coverage,
            gate=gate,
            parent_context=parent,
        )
        outcome = compute_decision(inputs, orch=self.orch, risk=risk)

        if outcome.action == DecisionAction.PERSIST:
            self._last[(symbol, timeframe)] = RecentDecision(
                direction=outcome.fused.direction,
                confidence=outcome.fused.confidence,
                valid_until=bucket_ts + timedelta(seconds=Timeframe.seconds(timeframe) * 4),
                bucket_ts=bucket_ts,
            )
        return outcome

    def _parent_context(
        self, *, symbol: str, timeframe: str, configured: list[str], now: datetime
    ) -> ContextInput | None:
        ordered = sorted(configured, key=Timeframe.rank)
        try:
            idx = ordered.index(timeframe)
        except ValueError:
            return None
        for parent_tf in ordered[idx + 1 :]:
            recent = self._last.get((symbol, parent_tf))
            if recent is None or recent.valid_until < now:
                continue
            if recent.direction == Direction.FLAT or recent.confidence <= 0:
                continue
            return ContextInput(direction=recent.direction, confidence=recent.confidence)
        return None


def _atr_of(technical: AgentSignal | None) -> float | None:
    if technical is None:
        return None
    raw = (technical.features or {}).get("atr14")
    return float(raw) if isinstance(raw, (int, float)) else None
