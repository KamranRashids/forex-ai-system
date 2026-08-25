"""Ingest pipeline: fetch -> gap-check -> upsert -> cache -> publish.

One :class:`IngestService` instance drives the worker loop. Every cycle:

1. For each (instrument, timeframe) compute the closed-bucket window to fetch:
   continue from the last stored bar (restart-safe) bounded by an initial
   history window and a per-cycle cap.
2. Call the provider through the circuit breaker.
3. Upsert bars idempotently; count provider-side gaps for observability.
4. Refresh the Redis latest-price quote and publish one ``bar.closed`` event
   to ``bars.closed.{tf}`` per newly inserted bar plus ``prices.live`` quotes.

SAFE MODE: this module stores market data and publishes analysis inputs —
it never emits orders or touches any execution path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bus.events import Event
from app.bus.publisher import EventPublisher, NullEventPublisher
from app.core.config import Settings
from app.data.breaker import BreakerOpenError, BreakerState, CircuitBreaker
from app.data.providers.base import (
    Candle,
    DataProvider,
    ProviderError,
    ProviderTransientError,
)
from app.data.repository import (
    get_or_create_instrument,
    last_closed_ts,
    upsert_candles,
)
from app.data.timeframes import Timeframe, iterate_buckets, previous_closed_bucket
from app.models.instrument import Instrument
from app.models.provider_health import ProviderHealth

logger = structlog.stdlib.get_logger(__name__)


@dataclass(slots=True)
class SeriesResult:
    """Outcome for one (symbol, timeframe) series within a cycle."""

    symbol: str
    timeframe: str
    inserted: int = 0
    updated: int = 0
    gaps_detected: int = 0
    skipped_reason: str | None = None


@dataclass(slots=True)
class CycleResult:
    ran_at: datetime
    results: list[SeriesResult] = field(default_factory=list)

    @property
    def inserted(self) -> int:
        return sum(r.inserted for r in self.results)

    @property
    def up_to_date(self) -> list[SeriesResult]:
        return [r for r in self.results if r.skipped_reason == "up_to_date"]

    @property
    def skipped_breaker(self) -> list[SeriesResult]:
        return [r for r in self.results if (r.skipped_reason or "").startswith("breaker:")]

    @property
    def failed(self) -> list[SeriesResult]:
        """Genuine failures: provider/transient/error skips (not benign skips)."""
        return [
            r
            for r in self.results
            if r.skipped_reason is not None
            and r.skipped_reason != "up_to_date"
            and not r.skipped_reason.startswith("breaker:")
        ]


class IngestService:
    """Drives a provider against the candle store through the breaker."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        provider: DataProvider,
        publisher: EventPublisher | None = None,
        breaker: CircuitBreaker | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = session_factory
        self._provider = provider
        self._publisher = publisher or NullEventPublisher()
        self.breaker = breaker or CircuitBreaker(
            provider_name=provider.name,
            failure_threshold=settings.provider_breaker_failure_threshold,
            cooldown_seconds=settings.provider_breaker_cooldown_seconds,
            clock=clock,
        )
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now().astimezone())

    # --- public API -----------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self._clock()

    async def run_cycle(self, instruments: list[Instrument], timeframes: list[str]) -> CycleResult:
        """Ingest every (instrument, timeframe) series once."""
        result = CycleResult(ran_at=self.now)
        for instrument in instruments:
            for timeframe in timeframes:
                result.results.append(await self._ingest_series(instrument, timeframe))
        return result

    async def run_backfill(
        self,
        instruments: list[Instrument],
        timeframes: list[str],
        *,
        start: datetime,
        end: datetime,
    ) -> CycleResult:
        """Backfill an explicit historical range (admin/CLI triggered)."""
        if end <= start:
            raise ValueError("backfill end must be after start")
        result = CycleResult(ran_at=self.now)
        for instrument in instruments:
            for timeframe in timeframes:
                result.results.append(
                    await self._ingest_series(
                        instrument, timeframe, forced_start=start, forced_end=end
                    )
                )
        return result

    # --- internals --------------------------------------------------------------

    def _fetch_window(
        self,
        last_ts: datetime | None,
        timeframe: str,
        now: datetime,
        *,
        forced_start: datetime | None = None,
        forced_end: datetime | None = None,
    ) -> tuple[datetime, datetime]:
        """Half-open [start, end) window of closed bucket starts to request."""
        step = timedelta(seconds=Timeframe.seconds(timeframe))
        end = forced_end or previous_closed_bucket(now, timeframe) + step

        if forced_start is not None:
            return forced_start, end

        if last_ts is None:
            history = timedelta(days=self._settings.ingest_initial_history_days)
            start = min(end - history, end - step)
        else:
            start = last_ts + step

        # Per-cycle cap bounds catch-ups after long outages; the remaining
        # backlog is drained over subsequent cycles.
        max_span = step * self._settings.ingest_max_bars_per_cycle
        if end - start > max_span:
            start = end - max_span
        return start, end

    async def _ingest_series(
        self,
        instrument: Instrument,
        timeframe: str,
        *,
        forced_start: datetime | None = None,
        forced_end: datetime | None = None,
    ) -> SeriesResult:
        outcome = SeriesResult(symbol=instrument.symbol, timeframe=timeframe)

        try:
            self.breaker.before_call()
        except BreakerOpenError as exc:
            outcome.skipped_reason = f"breaker:{exc}"
            return outcome

        try:
            async with self._sessions() as session:
                last_ts = await last_closed_ts(
                    session, instrument_id=instrument.id, timeframe=timeframe
                )
                start, end = self._fetch_window(
                    last_ts,
                    timeframe,
                    self.now,
                    forced_start=forced_start,
                    forced_end=forced_end,
                )
                if start >= end:
                    outcome.skipped_reason = "up_to_date"
                    self.breaker.record_success()
                    return outcome

                candles = [
                    c
                    for c in await self._provider.fetch_candles(
                        symbol=instrument.symbol,
                        timeframe=timeframe,
                        start=start,
                        end=end,
                    )
                    if c.complete
                ]

                expected = iterate_buckets(start, end, timeframe)
                returned = {c.bucket_start for c in candles}
                outcome.gaps_detected = len(set(expected) - returned)

                outcome.inserted, outcome.updated = await upsert_candles(
                    session,
                    instrument_id=instrument.id,
                    candles=candles,
                    source=self._provider.name,
                    timeframe=timeframe,
                )
                await session.commit()

            if candles:
                latest = max(candles, key=lambda c: c.bucket_start)
                await self._cache_quote(instrument, latest)
                await self._publish_new_bars(instrument, timeframe, candles)

            self.breaker.record_success()
            return outcome

        except ProviderTransientError as exc:
            opened = self.breaker.record_failure()
            suffix = " (breaker opened)" if opened else ""
            outcome.skipped_reason = f"transient:{exc}{suffix}"
            logger.warning(
                "ingest_provider_failure",
                symbol=instrument.symbol,
                timeframe=timeframe,
                error=str(exc),
                breaker_state=self.breaker.state.value,
            )
            await self.persist_breaker_snapshot()
            return outcome
        except ProviderError as exc:
            opened = self.breaker.record_failure()
            suffix = " (breaker opened)" if opened else ""
            outcome.skipped_reason = f"provider:{exc}{suffix}"
            logger.warning(
                "ingest_provider_error",
                symbol=instrument.symbol,
                timeframe=timeframe,
                error=str(exc),
                breaker_state=self.breaker.state.value,
            )
            await self.persist_breaker_snapshot()
            return outcome
        except Exception as exc:  # noqa: BLE001 - one bad series must not kill the worker
            outcome.skipped_reason = f"error:{type(exc).__name__}:{exc}"
            logger.exception(
                "ingest_unexpected_failure",
                symbol=instrument.symbol,
                timeframe=timeframe,
            )
            return outcome

    async def _cache_quote(self, instrument: Instrument, latest: Candle) -> None:
        """Refresh the Redis latest-quote cache and publish prices.live."""
        import json as _json

        quote = {
            "symbol": instrument.symbol,
            "price": float(latest.close),
            "bucket_start": latest.bucket_start.isoformat(),
            "synthetic": self._provider.name == "synthetic",
        }
        price_event = Event(
            event_type="price.tick",
            payload=quote,
            producer="ingest",
            produced_at=self.now,
        )
        try:
            await self._publisher.set_latest_price(
                instrument.symbol, _json.dumps(quote, separators=(",", ":"))
            )
            await self._publisher.publish_price(price_event)
        except Exception:  # noqa: BLE001 - cache/publish failures never fail ingestion
            logger.debug("price_cache_or_publish_failed", symbol=instrument.symbol)

    async def _publish_new_bars(
        self, instrument: Instrument, timeframe: str, candles: list[Candle]
    ) -> None:
        """Emit bar.closed events for newly stored bars (bounded burst)."""
        limit = self._settings.ingest_max_bars_per_cycle
        for candle in candles[:limit]:
            event = Event(
                event_type="bar.closed",
                payload={
                    "symbol": instrument.symbol,
                    "timeframe": timeframe,
                    "ts": candle.bucket_start.isoformat(),
                    "open": float(candle.open),
                    "high": float(candle.high),
                    "low": float(candle.low),
                    "close": float(candle.close),
                    "volume": int(candle.volume),
                    "source": self._provider.name,
                },
                producer="ingest",
                produced_at=self.now,
            )
            try:
                await self._publisher.publish_bar_closed(event, timeframe=timeframe)
            except Exception:  # noqa: BLE001 - stream unavailability must not halt ingestion
                logger.warning("bar_publish_failed", symbol=instrument.symbol)
                break

    # --- breaker persistence ------------------------------------------------------

    async def persist_breaker_snapshot(self) -> None:
        """Write breaker state into provider_health (restart-safe)."""
        async with self._sessions() as session:
            row = await session.get(ProviderHealth, self._provider.name)
            if row is None:
                row = ProviderHealth(provider=self._provider.name)
                session.add(row)
            if self.breaker.state is BreakerState.CLOSED:
                row.last_ok_at = self.now
            row.consecutive_failures = self.breaker.consecutive_failures
            row.breaker_state = self.breaker.state.value
            row.breaker_opened_at = self.breaker.opened_at
            row.updated_at = self.now
            await session.commit()

    async def load_breaker_snapshot(self) -> None:
        """Hydrate breaker from provider_health at startup."""
        async with self._sessions() as session:
            row = await session.get(ProviderHealth, self._provider.name)
            if row is not None:
                self.breaker.load(
                    state=row.breaker_state,
                    consecutive_failures=row.consecutive_failures,
                    opened_at=row.breaker_opened_at,
                )


async def seed_instruments(session: AsyncSession, symbols: list[str]) -> dict[str, Instrument]:
    """Seed/fetch instruments; used by worker startup, admin API, and CLI."""
    return {s: await get_or_create_instrument(session, s) for s in symbols}
