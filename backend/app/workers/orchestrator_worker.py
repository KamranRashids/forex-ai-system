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

SAFE MODE (L3): output is ANALYSIS / PAPER / BLOCKED paper-intent decisions.
There is no broker, order, or execution capability anywhere in this worker.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
)
from app.data.market_config import get_market_config
from app.data.risk_config import load_risk_params
from app.decisions.engine import DecideResult, DecisionAction, DecisionEngine, OrchParams
from app.models.decision import DecisionStatus

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


def _orch_params(settings: Settings) -> OrchParams:
    return OrchParams(
        coverage_min=settings.orch_min_agent_coverage,
        agreement_min=settings.orch_agreement_min,
        threshold=settings.orch_fusion_threshold,
        hysteresis=settings.orch_hysteresis,
        cooldown_seconds=settings.orch_pair_cooldown_seconds,
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
    ) -> None:
        self._sessions = session_factory
        self._redis = redis
        self._publisher = publisher
        self._settings = settings or get_settings()
        self._now = now or datetime.now(UTC)
        self._engine_cls = DecisionEngine
        self._lock_ttl = max(1, int(lock_ttl_seconds))
        self._owner_token: str = ""

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
            result = await engine.decide(
                symbol=symbol.upper(),
                timeframe=timeframe.upper(),
                configured=configured_timeframes,
                crafts=_orch_params(self._settings),
                risk=risk,
            )
            await session.commit()

        ORCH_DECISION_LATENCY.observe(time.perf_counter() - started)
        if result.action == DecisionAction.PERSIST:
            await self._emit_decision(result)
            if result.status == DecisionStatus.BLOCKED:
                await self._emit_risk_brake_alert(result)
        return result

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
