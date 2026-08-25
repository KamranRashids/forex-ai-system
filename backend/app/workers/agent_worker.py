"""Agent worker: consumes closed-bar events and runs the agent registry.

Stream contract (IMPLEMENTATION_PLAN §10):
- consumer group ``agents`` on every configured ``bars.closed.{tf}`` stream;
- backpressure: within a batch only the newest bar per (symbol, timeframe) is
  analyzed; older backlog entries are ACKed and counted as skipped;
- idempotency: signal identity (agent_id, symbol, timeframe, bucket_ts)
  makes replays no-ops at the persistence layer;
- every processed bar publishes ``signal.emitted`` events to
  ``signals.stream`` for the Phase 5 orchestrator.

SAFE MODE: analysis inputs in, analysis signals out — no order path exists.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.base import AnalysisContext, BaseAgent
from app.bus.events import Event
from app.bus.publisher import EventPublisher
from app.bus.topics import bars_closed_topic
from app.core.metrics import (
    AGENT_BAR_LATENCY,
    AGENT_BARS_SKIPPED_STALE,
    AGENT_SIGNALS_STORED,
    AGENT_SIGNALS_TOTAL,
)
from app.data.repository import get_or_create_instrument, load_candles
from app.data.signal_repository import new_run_id, save_signals

logger = structlog.stdlib.get_logger(__name__)

CONSUMER_GROUP: str = "agents"
CONSUMER_NAME: str = "agent-worker-1"
LOOKBACK_BARS: int = 150


@dataclass(slots=True)
class AgentBatchResult:
    processed: int = 0
    skipped_stale: int = 0
    signals_written: int = 0
    errors: int = 0
    latencies_ms: dict[str, float] = field(default_factory=dict)


class AgentWorker:
    """Runs the agent registry over each newly closed bar."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        publisher: EventPublisher,
        agents: list[BaseAgent],
        timeframes: list[str],
        clock: Callable[[], datetime] | None = None,
        lookback: int = LOOKBACK_BARS,
    ) -> None:
        self._sessions = session_factory
        self._redis = redis
        self._publisher = publisher
        self._agents = agents
        self.timeframes = [tf.upper() for tf in timeframes]
        self.lookback = lookback
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    # --- stream setup ---------------------------------------------------------

    async def ensure_groups(self) -> None:
        """Create the consumer group per stream; id '0' => consume history."""
        for timeframe in self.timeframes:
            try:
                await self._redis.xgroup_create(
                    bars_closed_topic(timeframe), CONSUMER_GROUP, id="0", mkstream=True
                )
            except Exception as exc:  # noqa: BLE001 - BUSYGROUP == already exists
                if "BUSYGROUP" not in str(exc):
                    raise

    async def poll_once(self, *, count: int = 50) -> AgentBatchResult:
        """Read one batch and analyze the newest bar per (symbol, timeframe)."""
        result = AgentBatchResult()
        streams: dict[str, str] = {bars_closed_topic(tf): ">" for tf in self.timeframes}
        # redis stubs demand bytes|int values for stream keys; str is valid at
        # runtime (decode_responses=True), hence the targeted ignore.
        response = await self._redis.xreadgroup(
            CONSUMER_GROUP,
            CONSUMER_NAME,
            streams,  # type: ignore[arg-type]
            count=count,
        )
        if not response:
            return result

        acks: list[tuple[str, str]] = []
        selected: dict[tuple[str, str], tuple[str, str, dict[str, str]]] = {}

        for stream_name, entries in response:
            for entry_id, fields in entries:
                payload = _unwrap(fields)
                if payload is None:  # unparseable envelope: ACK and count
                    acks.append((stream_name, entry_id))
                    AGENT_BARS_SKIPPED_STALE.inc()
                    result.skipped_stale += 1
                    continue
                key = (payload.get("symbol", ""), payload.get("timeframe", ""))
                previous = selected.get(key)
                if previous is None or str(payload.get("ts")) >= str(previous[2].get("ts")):
                    if previous is not None:
                        acks.append((stream_name, previous[0]))
                        AGENT_BARS_SKIPPED_STALE.inc()
                        result.skipped_stale += 1
                    selected[key] = (entry_id, stream_name, payload)
                else:
                    acks.append((stream_name, entry_id))
                    AGENT_BARS_SKIPPED_STALE.inc()
                    result.skipped_stale += 1

        for key, (entry_id, stream_name, fields) in list(selected.items()):
            try:
                written = await self.process_bar(fields)
                result.signals_written += written
                result.processed += 1
            except Exception as exc:  # noqa: BLE001 - poison bars never kill the loop
                result.errors += 1
                logger.exception("agent_bar_failed", error=str(exc), bar=_field(fields, "ts"))
                selected.pop(key, None)
            finally:
                acks.append((stream_name, entry_id))

        for stream_name, entry_id in acks:
            await self._redis.xack(stream_name, CONSUMER_GROUP, entry_id)
        return result

    async def process_bar(self, fields: dict[str, str]) -> int:
        """Run every registered agent over one closed bar; returns rows inserted."""
        started = time.perf_counter()
        symbol = _field(fields, "symbol").upper()
        timeframe = _field(fields, "timeframe")
        bucket_ts = datetime.fromisoformat(_field(fields, "ts"))
        run_id = new_run_id()

        candles, instrument = await self._load_series(symbol, timeframe)

        frame = pd.DataFrame(
            {
                "open": [float(r.open) for r in candles],
                "high": [float(r.high) for r in candles],
                "low": [float(r.low) for r in candles],
                "close": [float(r.close) for r in candles],
                "volume": [int(r.volume) for r in candles],
            },
            index=[r.ts for r in candles],
        )
        prev_daily = await self._prev_daily(instrument.id)

        signals = []
        for agent in self._agents:
            agent_start = time.perf_counter()
            ctx = AnalysisContext(
                symbol=symbol,
                timeframe=timeframe,
                bucket_ts=bucket_ts,
                candles=frame,
                now=self._clock(),
                meta={
                    "run_id": run_id,
                    "prev_daily": prev_daily,
                    "pip_size": float(instrument.pip_size),
                },
            )
            signal = agent.analyze(ctx)
            elapsed_ms = (time.perf_counter() - agent_start) * 1000
            AGENT_BAR_LATENCY.labels(agent=agent.id).observe(elapsed_ms / 1000.0)
            signals.append(signal)
            logger.debug(
                "agent_analyzed",
                agent=agent.id,
                symbol=symbol,
                latency_ms=round(elapsed_ms, 2),
            )

        async with self._sessions() as session:
            written = await save_signals(session, signals)
            await session.commit()

        AGENT_SIGNALS_STORED.inc(written)

        for signal in signals:
            event = Event(
                event_type="signal.emitted",
                payload={
                    "agent_id": signal.agent_id,
                    "version": signal.version,
                    "symbol": signal.symbol,
                    "timeframe": signal.timeframe,
                    "direction": signal.direction.value,
                    "confidence": float(signal.confidence),
                    "bucket_ts": signal.bucket_ts.isoformat(),
                    "valid_until": signal.valid_until.isoformat(),
                    "rationale": signal.rationale,
                },
                producer="agents",
                produced_at=self._clock(),
                correlation_id=run_id,
            )
            await self._publisher.publish_signal(event)
            AGENT_SIGNALS_TOTAL.labels(agent=signal.agent_id, outcome="emitted").inc()

        logger.info(
            "bar_processed",
            symbol=symbol,
            timeframe=timeframe,
            agents=len(self._agents),
            inserted=written,
            total_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return written

    # --- context builders -------------------------------------------------------

    async def _load_series(self, symbol: str, timeframe: str) -> tuple[list[Any], Any]:
        async with self._sessions() as session:
            instrument = await get_or_create_instrument(session, symbol)
            candles = await load_candles(
                session,
                instrument_id=instrument.id,
                timeframe=timeframe,
                limit=self.lookback,
            )
        return candles, instrument

    async def _prev_daily(self, instrument_id: Any) -> dict[str, float] | None:
        """Previous completed D1 candle (for floor pivots); None when absent."""
        from sqlalchemy import select

        from app.models.candle import CandleRow

        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(CandleRow)
                    .where(CandleRow.instrument_id == instrument_id, CandleRow.timeframe == "D1")
                    .order_by(CandleRow.ts.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        return {"high": float(row.high), "low": float(row.low), "close": float(row.close)}

    # --- runner -----------------------------------------------------------------

    async def run_forever(self, *, block_ms: int = 5_000) -> None:  # pragma: no cover
        await self.ensure_groups()
        while True:
            batch = await self.poll_once()
            if batch.processed == 0 and batch.skipped_stale == 0:
                await self._sleep(block_ms / 1000.0)

    async def _sleep(self, seconds: float) -> None:  # pragma: no cover
        import asyncio

        await asyncio.sleep(seconds)


def _field(fields: dict[str, str], key: str) -> str:
    raw: Any = fields.get(key)
    if isinstance(raw, bytes):
        return raw.decode()
    return str(raw or "")


def _unwrap(fields: dict[str, Any]) -> dict[str, str] | None:
    """Decode an ingest envelope ({"data": json}) into the bar payload."""
    raw = _field(fields, "data")
    if not raw:
        return None
    try:
        event = Event.from_json(raw)
    except Exception:  # noqa: BLE001 - malformed envelope: skip poison entry
        return None
    if event.event_type != "bar.closed":
        return None
    payload = {k: str(v) for k, v in event.payload.items()}
    return payload or None
