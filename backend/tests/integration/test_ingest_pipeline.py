"""Integration: ingest pipeline against real PostgreSQL + fakeredis bus."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from app.bus.events import Event
from app.data.breaker import BreakerState
from app.data.ingest import IngestService, seed_instruments
from app.data.providers.base import Candle, ProviderTransientError
from app.data.providers.factory import SyntheticProvider
from app.data.providers.synthetic import synthetic_candles
from app.data.repository import (
    count_candles,
    load_candles,
    upsert_candles,
)

pytestmark = [pytest.mark.integration]


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _friday_noon() -> datetime:
    # Friday => FX market open; synthetic generates regardless of hours.
    return datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _service(
    db_sessionmaker: Any,
    settings: Any,
    *,
    provider: Any = None,
    clock: FakeClock | None = None,
    fake_redis: Any = None,
) -> IngestService:
    from app.bus.publisher import RedisEventPublisher

    return IngestService(
        settings=settings,
        session_factory=db_sessionmaker,
        provider=provider or SyntheticProvider(),
        publisher=RedisEventPublisher(fake_redis) if fake_redis is not None else None,
        clock=clock,
    )


def _settings(**overrides: Any) -> Any:
    from app.core.config import Settings

    base = dict(
        secret_key="integration-secret-key-0123456789abcdef012345",
        market_data_symbols="EURUSD",
        market_data_timeframes="M15",
    )
    base.update(overrides)
    return Settings(**base)


class FlakyProvider(SyntheticProvider):
    """Raises transient errors for the first ``fail_times`` calls, then delegates."""

    def __init__(self, fail_times: int) -> None:
        self.remaining_failures = fail_times
        self.calls = 0

    async def fetch_candles(self, **kwargs: Any) -> list[Candle]:  # type: ignore[override]
        self.calls += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise ProviderTransientError("simulated outage")
        return await super().fetch_candles(**kwargs)


@pytest.mark.asyncio
async def test_seed_instruments_creates_rows(db_sessionmaker: Any) -> None:
    async with db_sessionmaker() as session:
        seeded = await seed_instruments(session, ["EURUSD", "USDJPY", "GBPUSD"])
        await session.commit()
    assert set(seeded) == {"EURUSD", "USDJPY", "GBPUSD"}
    assert seeded["EURUSD"].base == "EUR"
    assert str(seeded["USDJPY"].pip_size) == "0.01"


@pytest.mark.asyncio
async def test_candle_upsert_is_idempotent(db_sessionmaker: Any) -> None:
    start = _friday_noon() - timedelta(hours=24)
    end = start + timedelta(hours=8)
    candles = synthetic_candles("EURUSD", "M15", start, end)
    assert len(candles) == 32

    async with db_sessionmaker() as session:
        instrument = await seed_instruments(session, ["EURUSD"])
        await session.commit()
        instrument_id = instrument["EURUSD"].id

        inserted, updated = await upsert_candles(
            session,
            instrument_id=instrument_id,
            candles=candles,
            source="synthetic",
            timeframe="M15",
        )
        await session.commit()
        assert (inserted, updated) == (32, 0)

        again_i, again_u = await upsert_candles(
            session,
            instrument_id=instrument_id,
            candles=candles,
            source="synthetic",
            timeframe="M15",
        )
        await session.commit()
        assert (again_i, again_u) == (0, 32)

        total = await count_candles(session, instrument_id=instrument_id, timeframe="M15")
        assert total == 32


@pytest.mark.asyncio
async def test_complete_flag_never_regresses(db_sessionmaker: Any) -> None:
    bucket = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)

    async with db_sessionmaker() as session:
        instrument = await seed_instruments(session, ["EURUSD"])
        await session.commit()
        iid = instrument["EURUSD"].id

        complete_bar = Candle(
            symbol="EURUSD",
            timeframe="M15",
            bucket_start=bucket,
            open=Decimal("1.08500"),
            high=Decimal("1.08600"),
            low=Decimal("1.08400"),
            close=Decimal("1.08550"),
            volume=1000,
            complete=True,
        )
        await upsert_candles(
            session, instrument_id=iid, candles=[complete_bar], source="synthetic", timeframe="M15"
        )
        await session.commit()

        revised = Candle(
            symbol="EURUSD",
            timeframe="M15",
            bucket_start=bucket,
            open=complete_bar.open,
            high=complete_bar.high,
            low=complete_bar.low,
            close=Decimal("1.08580"),
            volume=1200,
            complete=False,
        )
        _, updated = await upsert_candles(
            session,
            instrument_id=iid,
            candles=[revised],
            source="synthetic",
            timeframe="M15",
        )
        await session.commit()
        assert updated == 1

        stored = (await load_candles(session, instrument_id=iid, timeframe="M15"))[0]
        assert stored.complete is True  # never downgraded
        assert str(stored.close) == "1.08580000"


@pytest.mark.asyncio
async def test_restart_continues_without_duplicates(db_sessionmaker: Any) -> None:
    clock = FakeClock(_friday_noon())
    settings = _settings(ingest_initial_history_days=1)

    service_a = _service(db_sessionmaker, settings, clock=clock)
    instruments = list((await _seed(db_sessionmaker, ["EURUSD"])).values())
    first = await service_a.run_cycle(instruments, ["M15"])

    clock.now += timedelta(hours=1)

    service_b = _service(db_sessionmaker, settings, clock=clock)  # fresh instance = restart
    second = await service_b.run_cycle(instruments, ["M15"])

    assert first.inserted > 0
    assert second.inserted == 4  # exactly four M15 bars in the advanced hour

    async with db_sessionmaker() as session:
        rows = await load_candles(session, instrument_id=instruments[0].id, timeframe="M15")
        timestamps = [r.ts for r in rows]
        assert len(timestamps) == len(set(timestamps))  # no duplicates
        assert rows[-1].ts == max(timestamps)
        # At T1=13:00 UTC the 12:45 bar has just closed.
        assert rows[-1].ts == datetime(2026, 8, 21, 12, 45, tzinfo=UTC)


@pytest.mark.asyncio
async def test_provider_outage_trips_breaker_then_recovers(
    db_sessionmaker: Any, fake_redis: Any
) -> None:
    clock = FakeClock(_friday_noon())
    settings = _settings(
        provider_breaker_failure_threshold=3,
        provider_breaker_cooldown_seconds=60,
    )
    flaky = FlakyProvider(fail_times=3)

    instruments = list((await _seed(db_sessionmaker, ["EURUSD"])).values())
    service = _service(
        db_sessionmaker, settings, provider=flaky, clock=clock, fake_redis=fake_redis
    )
    await service.load_breaker_snapshot()

    failed_cycle = await service.run_cycle(instruments, ["M15"])  # single series -> 1 failure
    failed_cycle_2 = await service.run_cycle(instruments, ["M15"])  # failure 2
    failed_cycle_3 = await service.run_cycle(instruments, ["M15"])  # failure 3 -> OPEN
    assert [r.skipped_reason for r in failed_cycle.results][0].startswith("transient:")
    assert failed_cycle_2.inserted == 0 and failed_cycle_3.inserted == 0
    assert service.breaker.state is BreakerState.OPEN

    # While open, cycles are refused outright.
    refused = await service.run_cycle(instruments, ["M15"])
    assert refused.skipped_breaker[0].skipped_reason.startswith("breaker:")
    assert not refused.failed

    # Breaker state survived into provider_health.
    row = await _health_row(db_sessionmaker, "synthetic")
    assert row is not None and row.breaker_state == "open"

    # Cooldown elapses -> half-open probe succeeds -> closed; data flows again.
    clock.now += timedelta(seconds=61)
    recovered = await service.run_cycle(instruments, ["M15"])
    assert recovered.inserted > 0
    assert service.breaker.state is BreakerState.CLOSED


@pytest.mark.asyncio
async def test_successful_cycle_publishes_streams_and_cache(
    db_sessionmaker: Any, fake_redis: Any
) -> None:
    clock = FakeClock(_friday_noon())
    settings = _settings(ingest_initial_history_days=1)
    instruments = list((await _seed(db_sessionmaker, ["EURUSD"])).values())

    service = _service(db_sessionmaker, settings, clock=clock, fake_redis=fake_redis)
    result = await service.run_cycle(instruments, ["M15"])
    assert result.inserted > 0

    entries = await fake_redis.xrange("bars.closed.M15")
    assert len(entries) >= result.inserted
    fields = entries[0][1]
    raw_event = fields["data"] if "data" in fields else fields[b"data"]
    first_event = Event.from_json(raw_event)
    assert first_event.event_type == "bar.closed"
    assert first_event.payload["symbol"] == "EURUSD"
    assert first_event.schema_version == 1

    cached = await fake_redis.get("prices.latest:EURUSD")
    quote = json.loads(cached)
    assert quote["symbol"] == "EURUSD"
    assert isinstance(quote["price"], float)


@pytest.mark.asyncio
async def test_staleness_alerts_fire_and_respect_market_hours(
    db_sessionmaker: Any, fake_redis: Any
) -> None:
    from app.monitor.staleness import StalenessMonitor

    # Saturday afternoon: market closed -> no breach even with zero bars.
    saturday = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)
    instruments = list((await _seed(db_sessionmaker, ["EURUSD", "GBPUSD", "USDJPY"])).values())
    monitor = StalenessMonitor(session_factory=db_sessionmaker, publisher=None)

    findings_weekend = await monitor.check(instruments, ["M15"], now=saturday)
    assert all(not f.breached for f in findings_weekend)

    # Wednesday with no data at all -> every series breaches.
    wednesday = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    findings_empty = await monitor.check(instruments, ["M15"], now=wednesday)
    assert all(f.breached for f in findings_empty)
    assert all(f.age_seconds is None for f in findings_empty)


async def _seed(db_sessionmaker: Any, symbols: list[str]) -> dict[str, Any]:
    async with db_sessionmaker() as session:
        seeded = await seed_instruments(session, symbols)
        await session.commit()
    return seeded


async def _health_row(db_sessionmaker: Any, provider: str) -> Any:
    from app.models.provider_health import ProviderHealth
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401

    async with db_sessionmaker() as session:
        return await session.get(ProviderHealth, provider)
