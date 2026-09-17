"""Orchestrator worker: turn fresh agent signals into decisions (Phase 5).

The orchestrator is the decision pipeline's driver:

- acquires the advisory ``lock:orchestrator`` (single active orchestrator);
- triggers on ``signal.emitted`` events on ``signals.stream`` (Redis is a
  *trigger* only — the engine re-reads the persisted ``agent_signals`` from
  the DB, which remains the source of truth, per approved Q4);
- runs a periodic full scan of the configured universe to recover from missed
  triggers;
- for each (symbol, timeframe) calls :class:`DecisionEngine` (fuse -> risk
  gate -> persist) and publishes ``decision.emitted`` to ``decisions.stream``.

(13C) When a ``broker_factory`` is injected, a decision freshly persisted as
``PAPER`` is wired inline (Option A) into the paper-only ``LedgerBroker`` in
the *same* transaction: decision + risk evaluation + PENDING order commit
together. ANALYSIS and BLOCKED decisions never reach the broker, and replayed
(collision) decisions never create a second order — the decision's unique
identity + ``uq_orders_paper_decision_id`` + one-OPEN-per-symbol constraints
make redelivery idempotent.

(13D) The PENDING order is filled by ``process_lifecycle()`` at its
deterministic next-bar open (§7): per active (symbol, timeframe) unit it
reconciles the ledger, cancels orders whose fill bar never closed, then drives
``PaperLifecycle.process_unit_bar`` per closed bar (restore -> fill/flip at
OPEN -> SL/TP at CLOSE -> mark -> snapshot -> commit), throttled to
``paper_catchup_max_bars`` per unit per cycle — ascending, never a skip. The
cycle runs under the same lock-guarded loop at ``paper_monitor_interval_seconds``
in ``orchestrator_runtime.py`` (single writer, per-unit-bar atomicity).

SAFE MODE (L3): output is ANALYSIS / PAPER / BLOCKED paper-intent decisions.
The only broker surface is the paper-only gateway (:class:`PaperBrokerGateway`);
the wiring exposes no live order-routing shape. Without a factory the worker
remains decision-only (and the lifecycle is a no-op).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.base import Direction
from app.broker.lifecycle import (
    CANCEL_MISSING_BAR,
    Bar,
    PaperLifecycle,
    catchup_window,
    missing_bar_candidates,
)
from app.bus.events import Event
from app.bus.publisher import EventPublisher
from app.bus.topics import (
    SIGNALS_STREAM,
    orchestrator_lock_key,
)
from app.core.config import Settings, get_settings
from app.core.metrics import (
    ORCH_CYCLE_COUNT,
    ORCH_DECISION_LATENCY,
    ORCH_DECISIONS_REPLAYED,
    PAPER_CATCHUP_BAR_BURST,
    PAPER_CATCHUP_BOUND_HITS_TOTAL,
    PAPER_CATCHUP_DEPTH_REMAINING,
    PAPER_LIFECYCLE_CYCLES_TOTAL,
    PAPER_LIFECYCLE_ERRORS_TOTAL,
    PAPER_OPEN_POSITIONS,
    PAPER_ORDERS_CANCELLED_TOTAL,
    PAPER_PENDING_EXPIRED_TOTAL,
    PAPER_RECONCILE_FAILS_TOTAL,
)
from app.data.market_config import get_market_config
from app.data.repository import get_or_create_instrument, last_closed_ts, load_candles
from app.data.risk_config import load_risk_params
from app.decisions.engine import DecideResult, DecisionAction, DecisionEngine, OrchParams, RiskLive
from app.decisions.ledger_gate import CANCEL_EXPIRED_PENDING, build_ledger_gate
from app.models.decision import DecisionStatus

if TYPE_CHECKING:
    from app.broker.ledger import LedgerBroker
    from app.broker.lifecycle import PendingCandidate
    from app.broker.positions import Position
    from app.models.decision import DecisionRow
    from app.models.paper_ledger import PaperOrderRow
    from app.models.risk_evaluation import RiskEvaluationRow

logger = structlog.stdlib.get_logger(__name__)

CONSUMER_GROUP: str = "orchestrator"
CONSUMER_NAME: str = "orchestrator-1"
LOCK_TTL_SECONDS: int = 120
SCAN_EVERY_CYCLES: int = 120


@dataclass(slots=True)
class OrchBatchResult:
    processed: int = 0
    replayed: int = 0
    errors: int = 0
    status_count: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class PaperLifecycleResult:
    """Aggregate counters for one ``process_lifecycle`` pass (Phase 13D)."""

    units: int = 0
    bars: int = 0
    fills: int = 0
    exits: int = 0
    superseded: int = 0
    cancelled_missing: int = 0
    expired: int = 0
    snapshots: int = 0
    depth_remaining: int = 0
    errors: int = 0
    reconcile_ok: bool = True


def _orch_params(settings: Settings) -> OrchParams:
    return OrchParams(
        coverage_min=settings.orch_min_agent_coverage,
        agreement_min=settings.orch_agreement_min,
        threshold=settings.orch_fusion_threshold,
        hysteresis=settings.orch_hysteresis,
        cooldown_seconds=settings.orch_pair_cooldown_seconds,
    )


class PaperBrokerGateway(Protocol):
    """Minimal broker surface the 13C decision wiring depends on (paper-only).

    Structural: exposes only the paper entry seam used here. It intentionally
    has no route/place/live-order member — a live-execution shape cannot be
    passed through this wiring. The only implementation is ``LedgerBroker``,
    which persists a PENDING order (no fill) the lifecycle resolves at the next
    bar's open.
    """

    async def submit_paper_order(
        self,
        *,
        symbol: str,
        timeframe: str,
        direction: Direction,
        ref_price: float,
        ts: datetime,
        units: float,
        decision_id: uuid.UUID | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> PaperOrderRow | None: ...


async def _wire_paper_decision(
    broker: PaperBrokerGateway | None,
    *,
    result: DecideResult,
    decision: DecisionRow | None,
    risk_eval: RiskEvaluationRow | None,
) -> PaperOrderRow | None:
    """Route a freshly persisted PAPER decision into the paper ledger (13C).

    Guards (fail closed):
    - only a decision this writer just stored (``created``) and that is
      explicitly ``PAPER`` may proceed — ANALYSIS and BLOCKED are no-ops;
    - the persisted risk evaluation must carry a valid paper sizing
      (``position_size_units`` and ``price``); missing sizing never creates an
      order (the risk gate is the single sizing authority — nothing here
      re-sizes or re-approves risk);
    - rejection by the broker (same-side position already open / FLAT /
      non-positive units) returns ``None`` without raising.

    Phase 13D: the persistent artifact is a **PENDING** paper order linked to
    the decision (``requested_price`` = the risk reference price, no fill, no
    position). The lifecycle fills it at the deterministic next-bar open.
    SAFE MODE: ``broker`` is a paper-only gateway; this function can create
    nothing but a paper order row fenced behind an approved PAPER decision.
    """
    if broker is None or result.status != DecisionStatus.PAPER or not result.created:
        return None
    if decision is None or risk_eval is None:
        return None
    if risk_eval.position_size_units is None or risk_eval.price is None:
        logger.debug(
            "paper_order_skipped_missing_sizing", symbol=result.symbol, timeframe=result.timeframe
        )
        return None
    direction = result.direction
    if direction is None:
        return None
    return await broker.submit_paper_order(
        symbol=result.symbol,
        timeframe=result.timeframe,
        direction=direction,
        ref_price=float(risk_eval.price),
        ts=result.bucket_ts,
        units=float(risk_eval.position_size_units),
        decision_id=decision.id,
        stop_loss=None if risk_eval.stop_loss is None else float(risk_eval.stop_loss),
        take_profit=None if risk_eval.take_profit is None else float(risk_eval.take_profit),
    )


class OrchestratorWorker:
    """Consumes signal triggers and drives the decision engine."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        publisher: EventPublisher,
        settings: Settings | None = None,
        now: datetime | None = None,
        lock_ttl_seconds: int = LOCK_TTL_SECONDS,
        broker_factory: Callable[[AsyncSession], LedgerBroker] | None = None,
    ) -> None:
        self._sessions = session_factory
        self._redis = redis
        self._publisher = publisher
        self._settings = settings or get_settings()
        self._now = now or datetime.now(UTC)
        self._engine_cls = DecisionEngine
        self._lock_ttl = max(1, int(lock_ttl_seconds))
        self._owner_token: str = ""
        #: (13C) Paper-only broker seam, bound per session inside ``process_pair``.
        #: ``None`` keeps the worker decision-only (default; used by existing
        #: tests and safe while no ledger wiring is desired).
        self._broker_factory = broker_factory

    @property
    def lock_ttl(self) -> int:
        """Lease duration (seconds) this orchestrator renews its lock for."""
        return self._lock_ttl

    async def acquire_lock(self) -> bool:
        """Try to become the active orchestrator (single-owner, fail closed).

        A unique owner token is stored with the lock. Only the holder that can
        prove ownership (``GET key == token``) may renew or release; this
        guarantees two orchestrators can never both believe they own the lock.
        Returns False when another orchestrator already holds the lock.
        """
        self._owner_token = uuid.uuid4().hex
        acquired = await self._redis.set(
            orchestrator_lock_key(),
            self._owner_token,
            nx=True,
            ex=self._lock_ttl,
        )
        return bool(acquired)

    async def renew_lock(self) -> bool:
        """Refresh the lock TTL iff we still own it (token-guarded).

        Uses a WATCH/MULTI/EXEC transaction so the expiry only happens when the
        key still holds our owner token. Returns True when ownership was
        confirmed and extended; False (fail closed) when the key is gone or held
        by another token — the caller MUST stop processing immediately.
        """
        if not self._owner_token:
            return False
        key = orchestrator_lock_key()
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                current = await pipe.get(key)
                if current != self._owner_token:
                    await pipe.unwatch()  # type: ignore[no-untyped-call]
                    return False
                pipe.multi()  # type: ignore[no-untyped-call]
                pipe.pexpire(key, self._lock_ttl * 1000)
                await pipe.execute()
        except Exception as exc:  # noqa: BLE001 - cannot confirm ownership
            logger.exception("orchestrator_lock_renew_failed", error=str(exc))
            return False
        return True

    async def release_lock(self) -> None:
        """Release the lock iff we still own it (graceful shutdown).

        Token-guarded — never deletes a lock we no longer own (e.g. another
        instance took over after our TTL expired).
        """
        if not self._owner_token:
            return
        key = orchestrator_lock_key()
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                current = await pipe.get(key)
                if current != self._owner_token:
                    await pipe.unwatch()  # type: ignore[no-untyped-call]
                    return
                pipe.multi()  # type: ignore[no-untyped-call]
                pipe.delete(key)
                await pipe.execute()
        except Exception:  # noqa: BLE001 - cleanup must not raise
            logger.debug("orchestrator_lock_release_failed")

    async def ensure_groups(self) -> None:
        try:
            await self._redis.xgroup_create(SIGNALS_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001 - BUSYGROUP == already exists
            if "BUSYGROUP" not in str(exc):
                raise

    async def process_pair(self, symbol: str, timeframe: str) -> DecideResult | None:
        """Compute + persist a decision for one (symbol, timeframe)."""
        started = time.perf_counter()
        async with self._sessions() as session:
            engine: DecisionEngine = self._engine_cls(session=session, now=datetime.now(UTC))
            risk = await load_risk_params(session, self._settings)
            configured_symbols, configured_timeframes = await get_market_config(
                session, self._settings
            )
            if (
                symbol.upper() not in configured_symbols
                or timeframe.upper() not in configured_timeframes
            ):
                return None
            # (14A) When a paper broker is wired, build it ONCE, restore the
            # ledger into it, derive the risk gate from the actual positions/
            # closed-trades, and reuse the same broker for the order sponsor —
            # the gate therefore reflects the ledger as of cycle start, before
            # this cycle's PENDING write (fill is next-bar, matching the driver).
            broker = None
            gate_view = None
            if self._broker_factory is not None:
                broker = self._broker_factory(session)
                await broker.restore_state()
                gate_view = await build_ledger_gate(
                    broker=broker, symbol=symbol.upper(), now=datetime.now(UTC)
                )
            result = await engine.decide(
                symbol=symbol.upper(),
                timeframe=timeframe.upper(),
                configured=configured_timeframes,
                crafts=_orch_params(self._settings),
                risk=risk,
                gate=gate_view.gate if gate_view is not None else None,
                live=RiskLive(
                    equity=gate_view.equity,
                    peak_equity=gate_view.peak_equity,
                    cumulative_realized=gate_view.cumulative_realized,
                )
                if gate_view is not None
                else None,
            )
            if result.action == DecisionAction.PERSIST:
                await self._sponsor_paper_order(session, result=result, broker=broker)
            await session.commit()

        ORCH_DECISION_LATENCY.observe(time.perf_counter() - started)
        if result.action == DecisionAction.PERSIST:
            await self._emit_decision(result)
            if result.status == DecisionStatus.BLOCKED:
                await self._emit_risk_brake_alert(result)
        return result

    async def _sponsor_paper_order(
        self,
        session: AsyncSession,
        *,
        result: DecideResult,
        broker: LedgerBroker | None = None,
    ) -> None:
        """Persist the paper order for a PAPER decision (13C / 13D).

        Runs inside the same transaction as the decision itself: the decision
        row, risk evaluation, and PENDING order all commit (or all roll back)
        together; the OPEN position is created later by the lifecycle's fill
        transaction. On any broker/database failure the exception propagates so
        the surrounding transaction is discarded — an order is never committed
        while the decision that sponsors it is rolled back.

        (14A) ``process_pair`` passes the broker it already restored for the
        risk gate, so the ledger is rehydrated exactly once per cycle; a fresh
        broker + ``restore_state()`` is only built when omitted (legacy callers).
        """
        if self._broker_factory is None:
            return
        if result.status != DecisionStatus.PAPER or not result.created:
            return
        decision, risk_eval = await self._load_decision_payload(session, result=result)
        if broker is None:
            broker = self._broker_factory(session)
            # Rehydrate the in-memory paper state from the persisted ledger before
            # checking the submission guard so any existing same-side position is
            # seen and the order is cleanly skipped instead of violating the keep
            # policy (mirror of the backtest driver's fill scheduling).
            await broker.restore_state()
        order = await _wire_paper_decision(
            broker, result=result, decision=decision, risk_eval=risk_eval
        )
        if order is not None:
            logger.info(
                "paper_order_submitted",
                decision_id=str(order.decision_id),
                symbol=order.symbol,
                timeframe=order.timeframe,
                order_id=str(order.id),
                side=order.side,
                status=order.status,
            )
        else:
            logger.info(
                "paper_order_not_created",
                symbol=result.symbol,
                timeframe=result.timeframe,
                status=result.status.value,
            )

    async def _emit_paper_alert(self, event_type: str, subject: str, detail: str) -> None:
        """Best-effort durable ``alert.paper_*`` sentinel (Phase 8 publisher)."""
        event = Event(
            event_type=event_type,
            payload={
                "source": "orchestrator",
                "severity": "warning",
                "subject": subject,
                "message": detail,
            },
            producer="orchestrator",
            produced_at=self._now,
        )
        try:
            await self._publisher.publish_alert(event)
        except Exception:  # noqa: BLE001 - alerting must never crash the pipeline
            logger.debug("paper_alert_publish_failed", event_type=event_type, subject=subject)

    async def process_lifecycle(self) -> PaperLifecycleResult | None:
        """Drive the paper lifecycle for every active unit (Phase 13D).

        Discovery session: restore the ledger into a fresh broker, reconcile it
        against the store (fail closed on mismatch), and derive the active
        (symbol, timeframe) units from resolvable pending orders + open
        positions. Then one fresh session per unit (avoids detached-entity
        updates on the same ``PaperLifecycle`` driver): cancel pending orders
        whose fill bar never closed (``missing_bar``), load the closed candles,
        and process outstanding bars ascending through
        ``PaperLifecycle.process_unit_bar`` — throttled to
        ``paper_catchup_max_bars`` (never a skip). Commits one unit-bar
        transaction at a time.

        Returns None when no broker factory is wired (decision-only mode);
        otherwise a per-cycle summary. Never raises: per-unit failures are
        counted, alerted, and skipped (cycle outcome ``degraded``).
        """
        if self._broker_factory is None:
            return None
        result = PaperLifecycleResult()
        max_bars = max(1, int(self._settings.paper_catchup_max_bars))
        snapshot_interval = self._settings.paper_snapshot_interval_seconds

        unit_list: list[tuple[str, str]] = []
        try:
            async with self._sessions() as session:
                broker = self._broker_factory(session)
                lifecycle = PaperLifecycle(store=broker.store, broker=broker)
                await broker.restore_state()
                if not await lifecycle.reconcile():
                    PAPER_RECONCILE_FAILS_TOTAL.inc()
                    result.reconcile_ok = False
                    logger.error(
                        "paper_lifecycle_reconcile_failed",
                        detail="store open/pending rows disagree with the restored broker",
                    )
                    await self._emit_paper_alert(
                        "alert.paper_reconcile",
                        "paper ledger/broker reconciliation failed",
                        "cycle aborted (fail closed)",
                    )
                    PAPER_LIFECYCLE_CYCLES_TOTAL.labels(outcome="reconcile_failed").inc()
                    return result
                units: dict[tuple[str, str], None] = {}
                for cand in await lifecycle.pending_candidates():
                    units[(cand.order.symbol, cand.order.timeframe)] = None
                for pos in await broker.store.list_open_positions():
                    units[(pos.symbol, pos.timeframe)] = None
                unit_list = sorted(units, key=lambda u: (u[0], u[1]))
        except Exception as exc:  # noqa: BLE001 - never kill the loop
            PAPER_LIFECYCLE_ERRORS_TOTAL.inc()
            logger.exception("paper_lifecycle_discovery_failed", error=str(exc))
            return result

        for symbol, timeframe in unit_list:
            try:
                (
                    processed,
                    depth,
                    fills,
                    exits,
                    superseded,
                    cancelled_missing,
                    snapshots,
                ) = await self._process_unit(
                    symbol=symbol,
                    timeframe=timeframe,
                    max_bars=max_bars,
                    snapshot_interval=snapshot_interval,
                )
                result.units += 1
                result.bars += processed
                result.depth_remaining += depth
                result.fills += fills
                result.exits += exits
                result.superseded += superseded
                result.cancelled_missing += cancelled_missing
                result.snapshots += snapshots
                if processed:
                    PAPER_CATCHUP_BAR_BURST.observe(processed)
            except Exception as exc:  # noqa: BLE001 - isolate one unit's failure
                result.errors += 1
                PAPER_LIFECYCLE_ERRORS_TOTAL.inc()
                logger.exception(
                    "paper_lifecycle_unit_failed",
                    error=str(exc),
                    symbol=symbol,
                    timeframe=timeframe,
                )
                await self._emit_paper_alert(
                    "alert.paper_lifecycle_degraded",
                    "paper lifecycle unit failed",
                    f"{symbol} {timeframe}: {exc}",
                )

        try:
            result.expired = await self._cancel_expired_pending()
        except Exception as exc:  # noqa: BLE001 - rollback the batch, count, continue
            result.errors += 1
            PAPER_LIFECYCLE_ERRORS_TOTAL.inc()
            logger.exception("paper_lifecycle_expiry_sweep_failed", error=str(exc))
            await self._emit_paper_alert(
                "alert.paper_lifecycle_degraded",
                "paper lifecycle expiry sweep failed",
                str(exc),
            )

        try:
            async with self._sessions() as session:
                broker = self._broker_factory(session)
                PAPER_OPEN_POSITIONS.set(len(await broker.store.list_open_positions()))
        except Exception:  # noqa: BLE001 - observability must not crash the cycle
            logger.debug("paper_open_positions_gauge_failed")

        outcome = "ok" if result.errors == 0 else "degraded"
        PAPER_LIFECYCLE_CYCLES_TOTAL.labels(outcome=outcome).inc()
        logger.info(
            "orchestrator_paper_lifecycle",
            outcome=outcome,
            units=result.units,
            bars=result.bars,
            fills=result.fills,
            exits=result.exits,
            superseded=result.superseded,
            cancelled_missing=result.cancelled_missing,
            expired=result.expired,
            snapshots=result.snapshots,
            depth_remaining=result.depth_remaining,
            errors=result.errors,
        )
        return result

    async def _process_unit(
        self,
        *,
        symbol: str,
        timeframe: str,
        max_bars: int,
        snapshot_interval: int | None,
    ) -> tuple[int, int, int, int, int, int, int]:
        """One unit's lifecycle pass.

        Returns ``(bars_processed, depth, fills, exits, superseded,
        cancelled_missing, snapshots)``.
        """
        if self._broker_factory is None:
            return 0, 0, 0, 0, 0, 0, 0
        async with self._sessions() as session:
            broker = self._broker_factory(session)
            lifecycle = PaperLifecycle(
                store=broker.store,
                broker=broker,
                snapshot_interval_seconds=snapshot_interval,
            )
            await broker.restore_state()

            candidates = [
                c
                for c in await lifecycle.pending_candidates()
                if c.order.symbol == symbol and c.order.timeframe == timeframe
            ]
            instrument = await get_or_create_instrument(session, symbol)
            latest_closed = await last_closed_ts(
                session, instrument_id=instrument.id, timeframe=timeframe
            )
            if latest_closed is None:
                return 0, 0, 0, 0, 0, 0, 0

            bases: list[datetime] = [
                c.fill_stamp for c in candidates if c.fill_stamp <= latest_closed
            ]
            current = broker.open_for(symbol)
            if current is not None:
                bases.append(current.entry_ts)

            in_window = await load_candles(
                session,
                instrument_id=instrument.id,
                timeframe=timeframe,
                start=min(bases) if bases else datetime.min.replace(tzinfo=UTC),
                end=latest_closed + timedelta(seconds=1),
                limit=100_000,
            )
            candles: dict[datetime, Bar] = {}
            for row in in_window:
                candles[row.ts] = Bar(ts=row.ts, open=float(row.open), close=float(row.close))

            doomed = missing_bar_candidates(candidates, list(candles), latest_closed=latest_closed)
            cancelled_missing = 0
            for cand in doomed:
                if await broker.cancel_pending(cand.order, reason=CANCEL_MISSING_BAR) is not None:
                    cancelled_missing += 1
                    PAPER_ORDERS_CANCELLED_TOTAL.labels(reason=CANCEL_MISSING_BAR).inc()
            if doomed:
                await session.commit()

            stamps = _outstanding_stamps(candidates=candidates, candles=candles, current=current)
            if not stamps:
                return 0, 0, 0, 0, 0, cancelled_missing, 0

            ordered = sorted(stamps)
            window, depth = catchup_window(ordered, max_bars)
            PAPER_CATCHUP_DEPTH_REMAINING.labels(symbol=symbol, timeframe=timeframe).set(depth)
            if depth:
                PAPER_CATCHUP_BOUND_HITS_TOTAL.inc()

            processed = fills = exits = superseded = snapshots = 0
            for ts in window:
                unit_result = await lifecycle.process_unit_bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    bar=candles[ts],
                    candidates=candidates,
                )
                await session.commit()
                processed += 1
                fills += unit_result.filled
                exits += unit_result.exits
                superseded += unit_result.superseded
                snapshots += 1 if unit_result.snapshot_written else 0

            return processed, depth, fills, exits, superseded, cancelled_missing, snapshots

    async def _cancel_expired_pending(self) -> int:
        """Deterministic PENDING-expiry sweep (14A).

        Cancels PENDING orders whose sponsoring decision passed its
        ``valid_until`` and never filled — releasing the one-open-per-symbol
        slot the way the backtest implies (no next open ⇒ no position). Runs
        after the per-unit missing-bar handling, in its own transaction; any
        failure rolls the whole batch back (PENDING stays PENDING) and retries
        next cycle. ``expired_pending`` rides the log/metric surface only — no
        ``cancel_reason`` column exists (migration-free by design).
        """
        now = datetime.now(UTC)
        if self._broker_factory is None:
            return 0
        async with self._sessions() as session:
            broker = self._broker_factory(session)
            await broker.restore_state()
            expired = await broker.list_pending_expired(now=now)
            for order in expired:
                try:
                    if (
                        await broker.cancel_pending(order, reason=CANCEL_EXPIRED_PENDING)
                        is not None
                    ):
                        PAPER_PENDING_EXPIRED_TOTAL.inc()
                        logger.info(
                            "paper_order_cancelled",
                            order_id=str(order.id),
                            symbol=order.symbol,
                            timeframe=order.timeframe,
                            status=order.status,
                            reason=CANCEL_EXPIRED_PENDING,
                        )
                except Exception as exc:  # noqa: BLE001 - roll back the batch
                    logger.exception(
                        "paper_order_cancel_failed",
                        order_id=str(order.id),
                        reason=CANCEL_EXPIRED_PENDING,
                        error=str(exc),
                    )
                    raise
            await session.commit()
            return len(expired)

    async def _load_decision_payload(
        self, session: AsyncSession, *, result: DecideResult
    ) -> tuple[DecisionRow | None, RiskEvaluationRow | None]:
        """Load the decision + risk evaluation this writer just persisted."""
        from sqlalchemy import select

        from app.models.decision import DecisionRow
        from app.models.risk_evaluation import RiskEvaluationRow

        decision = (
            (
                await session.execute(
                    select(DecisionRow).where(
                        DecisionRow.symbol == result.symbol,
                        DecisionRow.timeframe == result.timeframe,
                        DecisionRow.bucket_ts == result.bucket_ts,
                    )
                )
            )
            .scalars()
            .first()
        )
        risk_eval = (
            (
                await session.execute(
                    select(RiskEvaluationRow).where(
                        RiskEvaluationRow.symbol == result.symbol,
                        RiskEvaluationRow.timeframe == result.timeframe,
                        RiskEvaluationRow.bucket_ts == result.bucket_ts,
                    )
                )
            )
            .scalars()
            .first()
        )
        return decision, risk_eval

    async def _emit_risk_brake_alert(self, result: DecideResult) -> None:
        """Surface a risk-gate veto as a durable ``alert.risk_brake`` (Phase 8)."""
        event = Event(
            event_type="alert.risk_brake",
            payload={
                "source": "risk",
                "severity": "warning",
                "symbol": result.symbol,
                "timeframe": result.timeframe,
                "bucket_ts": result.bucket_ts.isoformat() if result.bucket_ts else "",
                "veto_code": result.veto_code or "blocked",
                "direction": result.direction.value if result.direction else "FLAT",
            },
            producer="orchestrator",
            produced_at=self._now,
        )
        try:
            await self._publisher.publish_alert(event)
        except Exception:  # noqa: BLE001 - alerting must never crash the pipeline
            logger.debug("risk_brake_alert_publish_failed", symbol=result.symbol)

    async def _emit_decision(self, result: DecideResult) -> None:
        event = Event(
            event_type="decision.emitted",
            payload={
                "symbol": result.symbol,
                "timeframe": result.timeframe,
                "bucket_ts": result.bucket_ts.isoformat() if result.bucket_ts else "",
                "direction": result.direction.value if result.direction else "FLAT",
                "status": result.status.value if result.status else "ANALYSIS",
                "confidence": round(result.confidence, 4),
                "agreement": round(result.agreement, 4),
                "coverage": round(result.coverage, 4),
                "veto_code": result.veto_code or "",
                "inputs_hash": result.inputs_hash,
            },
            producer="orchestrator",
            produced_at=self._now,
        )
        await self._publisher.publish_decision(event)

    async def poll_once(self, *, count: int = 50) -> OrchBatchResult:
        """Read one batch of ``signal.emitted`` triggers and process pairs."""
        result = OrchBatchResult()
        streams: dict[str, str] = {SIGNALS_STREAM: ">"}
        response = await self._redis.xreadgroup(
            CONSUMER_GROUP,
            CONSUMER_NAME,
            streams,  # type: ignore[arg-type]
            count=count,
        )
        if not response:
            return result

        acks: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for stream_name, entries in response:
            for entry_id, fields in entries:
                payload = _unwrap(fields)
                if payload is None:
                    acks.append((stream_name, entry_id))
                    result.replayed += 1
                    continue
                symbol = payload.get("symbol", "")
                timeframe = payload.get("timeframe", "")
                key = (symbol, timeframe)
                if not symbol or not timeframe:
                    acks.append((stream_name, entry_id))
                    result.replayed += 1
                    continue
                if key in seen:  # latest-per-pair backpressure within a batch
                    acks.append((stream_name, entry_id))
                    result.replayed += 1
                    continue
                seen.add(key)
                try:
                    outcome = await self.process_pair(symbol, timeframe)
                    if outcome is None:
                        acks.append((stream_name, entry_id))
                        result.replayed += 1
                        continue
                    if not outcome.created:
                        ORCH_DECISIONS_REPLAYED.inc()
                        result.replayed += 1
                    if outcome.status:
                        result.status_count[outcome.status.value] = (
                            result.status_count.get(outcome.status.value, 0) + 1
                        )
                    result.processed += 1
                    acks.append((stream_name, entry_id))
                except Exception as exc:  # noqa: BLE001 - never kill the loop
                    result.errors += 1
                    logger.exception(
                        "orchestrator_pair_failed", error=str(exc), symbol=symbol, tf=timeframe
                    )
                    acks.append((stream_name, entry_id))

        for stream_name, entry_id in acks:
            await self._redis.xack(stream_name, CONSUMER_GROUP, entry_id)
        return result

    async def scan_all(self) -> OrchBatchResult:
        """Catch-up: process every configured pair regardless of triggers."""
        result = OrchBatchResult()
        ORCH_CYCLE_COUNT.labels(outcome="scan").inc()
        async with self._sessions() as session:
            configured_symbols, configured_timeframes = await get_market_config(
                session, self._settings
            )
        for symbol in configured_symbols:
            for timeframe in configured_timeframes:
                outcome = await self.process_pair(symbol, timeframe)
                if outcome is None:
                    continue
                if outcome.action == DecisionAction.PERSIST:
                    result.processed += 1
                    if outcome.status:
                        result.status_count[outcome.status.value] = (
                            result.status_count.get(outcome.status.value, 0) + 1
                        )
        return result


def _field(fields: dict[str, str], key: str) -> str:
    raw: Any = fields.get(key)
    if isinstance(raw, bytes):
        return raw.decode()
    return str(raw or "")


def _outstanding_stamps(
    *,
    candidates: Sequence[PendingCandidate],
    candles: Mapping[datetime, Bar],
    current: Position | None,
) -> set[datetime]:
    """Closed bars this unit still owes processing to (fill bars + continuation).

    Tail semantics: everything from the *earliest* anchor onward. The anchor is
    the earliest due fill bar (a PENDING order whose fill bar has closed) or the
    open position's entry — whichever is earlier. Because processing is
    ascending, the bars that fall between an anchor and a later fill are
    harmless no-ops, and a fill creates continuation bars that are already >=
    the anchor (>= the fill bar). A fill stamp beyond the newest closed bar is
    simply not due yet and excluded.
    """
    due = sorted(c.fill_stamp for c in candidates if c.fill_stamp in candles)
    fill_anchor = due[0] if due else None
    anchors: list[datetime] = [fill_anchor] if fill_anchor is not None else []
    if current is not None:
        anchors.append(current.entry_ts)
    if not anchors:
        return set()
    anchor = min(anchors)
    return {ts for ts in candles if ts >= anchor}


def _unwrap(fields: dict[str, Any]) -> dict[str, str] | None:
    """Decode a signal.envelope into 'signal.emitted' payload, or None."""
    raw = _field(fields, "data")
    if not raw:
        return None
    try:
        event = Event.from_json(raw)
    except Exception:  # noqa: BLE001 - malformed envelope: skip poison entry
        return None
    if event.event_type != "signal.emitted":
        return None
    return {k: str(v) for k, v in event.payload.items()} or None
