"""Orchestrator decision engine (Phase 5): fuse -> risk-gate -> persist.

Two layers:

- :func:`compute_decision` is a pure, synchronous transformation from a
  :class:`DecisionInputs` snapshot to a :class:`DecisionOutcome`. It is
  fully unit-testable without a database (deterministic).
- :class:`DecisionEngine.decide` fetches the inputs from the DB (signals,
  candles, parent-TF context, risk collisions), calls the pure function, and
  persists decisions + risk evaluations idempotently (first-writer-wins on
  (symbol, timeframe, bucket_ts)).

SAFE MODE (L3): output is ANALYSIS / PAPER / BLOCKED only. Nothing here can
place or route an order; `PAPER` is a paper intent, and only reaches PAPER
when *every* risk gate passes (fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog
from app.agents.base import AgentSignal, Direction
from app.core.metrics import ORCH_DECISIONS_TOTAL, RISK_BLOCKED_TOTAL
from app.data.decision_repository import (
    decision_values,
    load_active_paper_snapshot,
    load_latest_decision,
    load_risk_state,
    save_decision,
    save_risk_evaluation,
    upsert_risk_state,
)
from app.data.risk_config import RiskParams, load_risk_params
from app.data.signal_repository import load_latest_per_agent, row_to_signal
from app.data.timeframes import Timeframe
from app.decisions.fusion import (
    ContextInput,
    FusionParams,
    apply_context,
    fuse,
)
from app.decisions.risk import (
    GateState,
    RiskDeps,
    RiskOutcome,
    assess,
)
from app.models.decision import DecisionStatus
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.stdlib.get_logger(__name__)

AGENT_FAMILIES: tuple[str, ...] = ("technical", "regime", "fundamental", "sentiment")


@dataclass(frozen=True, slots=True)
class OrchParams:
    """Orchestrator tuning (resolved from Settings env)."""

    coverage_min: float = 0.5
    agreement_min: float = 0.5
    threshold: float = 0.15
    hysteresis: float = 0.04
    context_weight: float = 0.15
    cooldown_seconds: int = 1800
    weights: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DecisionInputs:
    """All inputs a decision needs (bundled for pure computation)."""

    symbol: str
    timeframe: str
    bucket_ts: datetime
    now: datetime
    signals: dict[str, AgentSignal]
    regime: str
    atr: float | None
    price: float | None
    coverage: float
    gate: GateState
    parent_context: ContextInput | None = None


class DecisionAction(StrEnum):
    SKIP = "SKIP"
    PERSIST = "PERSIST"


class SkipReason(StrEnum):
    NO_VOTES = "no_directional_votes"
    INSUFFICIENT_COVERAGE = "insufficient_coverage"
    WEAK_FLAT = "weak_flat"


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    action: DecisionAction
    fused: Any
    status: DecisionStatus | None = None
    risk: RiskOutcome | None = None
    coverage: float = 0.0
    skip_reason: SkipReason | None = None
    veto_code: str | None = None
    veto_reason: str | None = None


def _coverage(signals: dict[str, AgentSignal]) -> float:
    present = sum(1 for a in AGENT_FAMILIES if signals.get(a) is not None)
    return present / len(AGENT_FAMILIES)


def _first_failing(name: str, outcome: RiskOutcome) -> tuple[str, str] | None:
    gate = outcome.gates.get(name)
    if gate is not None and not gate.ok:
        return name, gate.detail
    return None


def compute_decision(
    inputs: DecisionInputs,
    *,
    orch: OrchParams,
    risk: RiskParams,
) -> DecisionOutcome:
    """Build a decision from a snapshot. Pure and deterministic."""
    fused = fuse(
        inputs.signals,
        regime=inputs.regime,
        params=FusionParams(
            threshold=orch.threshold,
            hysteresis=orch.hysteresis,
            **({"weights": orch.weights} if orch.weights else {}),
        ),
    )
    if inputs.parent_context is not None:
        fused = apply_context(fused, inputs.parent_context)

    if not fused.has_votes:
        return DecisionOutcome(
            action=DecisionAction.SKIP, fused=fused, skip_reason=SkipReason.NO_VOTES
        )
    if inputs.coverage < orch.coverage_min:
        return DecisionOutcome(
            action=DecisionAction.SKIP,
            fused=fused,
            coverage=inputs.coverage,
            skip_reason=SkipReason.INSUFFICIENT_COVERAGE,
        )
    if fused.direction == Direction.FLAT:
        return DecisionOutcome(
            action=DecisionAction.SKIP,
            fused=fused,
            coverage=inputs.coverage,
            skip_reason=SkipReason.WEAK_FLAT,
        )

    risk_outcome = assess(
        direction=fused.direction,
        deps=RiskDeps(atr=inputs.atr, price=inputs.price),
        gate=inputs.gate,
        params=risk,
    )
    promotable = fused.agreement >= orch.agreement_min

    if not promotable:
        status = DecisionStatus.ANALYSIS
        veto_code, veto_reason = "low_agreement", f"agreement={fused.agreement:.3f}"
    elif risk_outcome.passed and risk_outcome.paper:
        status = DecisionStatus.PAPER
        veto_code, veto_reason = None, None
    elif risk_outcome.passed:
        status = DecisionStatus.ANALYSIS
        veto_code, veto_reason = "risk_disabled", "risk gate disabled"
    else:
        status = DecisionStatus.BLOCKED
        veto_code, veto_reason = _first_failing("rr", risk_outcome) or (
            _first_failing("exposure", risk_outcome)
            or _first_failing("correlation", risk_outcome)
            or _first_failing("daily_loss", risk_outcome)
            or _first_failing("drawdown", risk_outcome)
            or ("risk", "risk gate failed")
        )

    return DecisionOutcome(
        action=DecisionAction.PERSIST,
        fused=fused,
        status=status,
        risk=risk_outcome,
        coverage=inputs.coverage,
        veto_code=veto_code,
        veto_reason=veto_reason,
    )


@dataclass(frozen=True, slots=True)
class DecideResult:
    """Outcome of one (re)attempted decision, for the caller and tests."""

    symbol: str
    timeframe: str
    bucket_ts: datetime
    action: DecisionAction
    created: bool
    status: DecisionStatus | None
    direction: Direction | None
    confidence: float
    agreement: float
    coverage: float
    veto_code: str | None
    skip_reason: SkipReason | None
    inputs_hash: str = ""


async def _resolve_atr_price(
    session: AsyncSession, *, symbol: str, timeframe: str, bucket_ts: datetime
) -> tuple[float | None, float | None]:
    """ATR/price from the technical signal + latest stored close (both best-effort)."""
    technical = await load_latest_per_agent(
        session, symbol=symbol, timeframe=timeframe, agent_id="technical"
    )
    atr: float | None = None
    price: float | None = None
    if technical:
        signal = row_to_signal(technical[0])
        raw = (signal.features or {}).get("atr14")
        if isinstance(raw, (int, float)):
            atr = float(raw)

    from app.data.repository import get_or_create_instrument
    from app.models.candle import CandleRow
    from sqlalchemy import select

    instrument = await get_or_create_instrument(session, symbol)
    candle = (
        await session.execute(
            select(CandleRow)
            .where(
                CandleRow.instrument_id == instrument.id,
                CandleRow.timeframe == timeframe,
                CandleRow.ts <= bucket_ts,
            )
            .order_by(CandleRow.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if candle is not None:
        price = float(candle.close)
    return atr, price


async def _gate_state(
    session: AsyncSession, *, symbol: str, now: datetime, equity: float
) -> GateState:
    """Compute live exposure/correlation + risk_state daily-loss/drawdown."""
    daily_key = now.astimezone(UTC).strftime("%Y-%m-%d")
    daily = await load_risk_state(session, scope="daily", period_key=daily_key)
    account = await load_risk_state(session, scope="account", period_key="global")

    snapshots = await load_active_paper_snapshot(session, now=now)
    total_notional = sum(s.notional for s in snapshots)
    target_basket = {symbol[:3], symbol[3:]}
    basket_notional = sum(s.notional for s in snapshots if s.basket & target_basket)
    correlation_triggered = any(s.basket & target_basket for s in snapshots)
    exposure_pct = total_notional / equity if equity else 0.0
    correlation_pct = basket_notional / equity if equity else 0.0

    return GateState(
        exposure_used_pct=exposure_pct,
        correlation_used_pct=correlation_pct,
        daily_loss_used_pct=float(daily.realized_loss) if daily else 0.0,
        drawdown_used_pct=float(account.max_drawdown) if account else 0.0,
        correlation_triggered=correlation_triggered,
    )


async def _parent_context(
    session: AsyncSession, *, symbol: str, timeframe: str, configured: list[str], now: datetime
) -> ContextInput | None:
    """Nearest larger configured timeframe's fresh decision as context."""
    ordered = sorted(configured, key=Timeframe.rank)
    try:
        idx = ordered.index(timeframe)
    except ValueError:
        return None
    for parent_tf in ordered[idx + 1 :]:
        parent = await load_latest_decision(session, symbol=symbol, timeframe=parent_tf, now=now)
        if parent is None:
            continue
        direction = Direction(parent.fused_direction)
        if direction == Direction.FLAT or parent.confidence <= 0:
            continue
        return ContextInput(direction=direction, confidence=float(parent.confidence))
    return None


class DecisionEngine:
    """DB-aware wrapper: fetch inputs, compute, persist, publish aggregate state."""

    def __init__(self, *, session: AsyncSession, now: datetime | None = None) -> None:
        self._session = session
        self._now = now or datetime.now(UTC)

    async def decide(
        self,
        *,
        symbol: str,
        timeframe: str,
        configured: list[str],
        crafts: OrchParams,
        risk: RiskParams,
    ) -> DecideResult:
        started = datetime.now(UTC)

        signals = await load_latest_per_agent(
            self._session, symbol=symbol, timeframe=timeframe, now=self._now
        )
        if not signals:
            return DecideResult(
                symbol=symbol,
                timeframe=timeframe,
                bucket_ts=started,
                action=DecisionAction.SKIP,
                created=False,
                status=None,
                direction=None,
                confidence=0.0,
                agreement=0.0,
                coverage=0.0,
                veto_code=None,
                skip_reason=SkipReason.NO_VOTES,
            )

        by_agent: dict[str, AgentSignal] = {}
        for row in signals:
            sig = row_to_signal(row)
            by_agent[sig.agent_id] = sig

        latest_bucket = max((s.bucket_ts for s in by_agent.values()), default=started)
        at_bucket = {a: s for a, s in by_agent.items() if s.bucket_ts == latest_bucket}
        if not at_bucket:
            return DecideResult(
                symbol=symbol,
                timeframe=timeframe,
                bucket_ts=latest_bucket,
                action=DecisionAction.SKIP,
                created=False,
                status=None,
                direction=None,
                confidence=0.0,
                agreement=0.0,
                coverage=0.0,
                veto_code=None,
                skip_reason=SkipReason.NO_VOTES,
            )

        coverage = _coverage(at_bucket)
        regime_sig = at_bucket.get("regime")
        regime = (
            str((regime_sig.features or {}).get("regime", "unknown")) if regime_sig else "unknown"
        )
        atr, price = await _resolve_atr_price(
            self._session, symbol=symbol, timeframe=timeframe, bucket_ts=latest_bucket
        )
        gate = await _gate_state(
            self._session, symbol=symbol, now=self._now, equity=risk.paper_equity
        )
        parent = await _parent_context(
            self._session, symbol=symbol, timeframe=timeframe, configured=configured, now=self._now
        )

        inputs = DecisionInputs(
            symbol=symbol,
            timeframe=timeframe,
            bucket_ts=latest_bucket,
            now=self._now,
            signals=at_bucket,
            regime=regime,
            atr=atr,
            price=price,
            coverage=coverage,
            gate=gate,
            parent_context=parent,
        )
        outcome = compute_decision(inputs, orch=crafts, risk=risk)

        if outcome.action == DecisionAction.SKIP:
            return DecideResult(
                symbol=symbol,
                timeframe=timeframe,
                bucket_ts=latest_bucket,
                action=outcome.action,
                created=False,
                status=None,
                direction=outcome.fused.direction if outcome.fused.has_votes else None,
                confidence=outcome.fused.confidence,
                agreement=outcome.fused.agreement,
                coverage=coverage,
                veto_code=None,
                skip_reason=outcome.skip_reason,
            )

        status = outcome.status or DecisionStatus.ANALYSIS
        # Cooldown: suppress flapping paper intents.
        if status == DecisionStatus.PAPER and crafts.cooldown_seconds > 0:
            recent = await load_latest_decision(
                self._session,
                symbol=symbol,
                timeframe=timeframe,
                now=self._now - timedelta(seconds=crafts.cooldown_seconds),
            )
            if recent is not None and recent.fused_direction != Direction.FLAT.value:
                status = DecisionStatus.ANALYSIS
                outcome = DecisionOutcome(
                    action=outcome.action,
                    fused=outcome.fused,
                    status=status,
                    risk=outcome.risk,
                    coverage=coverage,
                    veto_code="cooldown",
                    veto_reason="recent decision within cooldown window",
                )

        from app.decisions.hashing import inputs_hash

        hash_value = inputs_hash(
            symbol=symbol,
            timeframe=timeframe,
            bucket_ts=latest_bucket.isoformat(),
            agent_versions={a: s.version for a, s in at_bucket.items()},
            weights=outcome.fused.weights,
        )
        values = decision_values(
            run_id="",
            symbol=symbol,
            timeframe=timeframe,
            bucket_ts=latest_bucket,
            fused=outcome.fused,
            status=status,
            veto_code=outcome.veto_code,
            veto_reason=outcome.veto_reason,
            inputs_hash=hash_value,
            code_versions={a: s.version for a, s in at_bucket.items()},
            rationale=_rationale(outcome),
            valid_until=latest_bucket + timedelta(seconds=Timeframe.seconds(timeframe) * 4),
        )
        created = await save_decision(self._session, values)
        decision_id = None
        if outcome.risk is not None:
            if created:
                decision_id = await self._latest_decision_id(symbol, timeframe, latest_bucket)
            await save_risk_evaluation(
                self._session,
                decision_id=decision_id,
                symbol=symbol,
                timeframe=timeframe,
                bucket_ts=latest_bucket,
                outcome=outcome.risk,
            )
        if created:
            ORCH_DECISIONS_TOTAL.labels(status=status.value).inc()
            if status == DecisionStatus.BLOCKED and outcome.veto_code:
                RISK_BLOCKED_TOTAL.labels(gate=outcome.veto_code).inc()

        await self._refresh_exposure()
        await self._session.flush()
        return DecideResult(
            symbol=symbol,
            timeframe=timeframe,
            bucket_ts=latest_bucket,
            action=outcome.action,
            created=created,
            status=status,
            direction=outcome.fused.direction,
            confidence=outcome.fused.confidence,
            agreement=outcome.fused.agreement,
            coverage=coverage,
            veto_code=outcome.veto_code,
            skip_reason=None,
            inputs_hash=hash_value,
        )

    async def _latest_decision_id(self, symbol: str, timeframe: str, bucket_ts: datetime) -> Any:
        from app.models.decision import DecisionRow
        from sqlalchemy import select

        row = (
            await self._session.execute(
                select(DecisionRow.id).where(
                    DecisionRow.symbol == symbol.upper(),
                    DecisionRow.timeframe == timeframe,
                    DecisionRow.bucket_ts == bucket_ts,
                )
            )
        ).scalar_one_or_none()
        return row

    async def _refresh_exposure(self) -> None:
        """Persist a fresh aggregate account/daily exposure for observability."""
        snapshots = await load_active_paper_snapshot(self._session, now=self._now)
        total_notional = sum(s.notional for s in snapshots)
        daily_key = self._now.astimezone(UTC).strftime("%Y-%m-%d")
        daily = await load_risk_state(self._session, scope="daily", period_key=daily_key)
        account = await load_risk_state(self._session, scope="account", period_key="global")

        from app.core.config import get_settings

        risk = await load_risk_params(self._session, get_settings())
        equity = risk.paper_equity if risk.paper_equity else 100_000.0
        exposure_pct = total_notional / equity if equity else 0.0

        await upsert_risk_state(
            self._session,
            scope="account",
            period_key="global",
            realized_loss=float(account.realized_loss) if account else 0.0,
            peak_equity=float(account.peak_equity) if account else equity,
            max_drawdown=float(account.max_drawdown) if account else 0.0,
            exposure=exposure_pct,
        )
        await upsert_risk_state(
            self._session,
            scope="daily",
            period_key=daily_key,
            realized_loss=float(daily.realized_loss) if daily else 0.0,
            exposure=exposure_pct,
        )


def _rationale(outcome: DecisionOutcome) -> str:
    status = outcome.status.value if outcome.status else "?"
    parts = [f"status={status}"]
    if outcome.veto_code:
        parts.append(f"veto={outcome.veto_code}: {outcome.veto_reason}")
    return " ".join(parts)
