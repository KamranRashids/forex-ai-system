"""Unit tests for the agent contract: signal model, freshness, registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from app.agents.base import AgentSignal, AnalysisContext, BaseAgent, Direction
from app.agents.base import valid_until_for as vf
from app.agents.registry import AgentRegistry
from pydantic import ValidationError


def _ctx(bars: int = 60) -> AnalysisContext:
    idx = pd.date_range("2026-08-21T09:00:00Z", periods=bars, freq="15min")
    frame = pd.DataFrame(
        {
            "open": np_range(1.0, bars),
            "high": np_range(2.0, bars),
            "low": np_range(0.0, bars),
            "close": np_range(1.5, bars),
            "volume": [100] * bars,
        },
        index=idx,
    )
    return AnalysisContext(
        symbol="EURUSD",
        timeframe="M15",
        bucket_ts=idx[-1],
        candles=frame,
        now=idx[-1].to_pydatetime().replace(tzinfo=UTC),
    )


def np_range(start: float, n: int) -> list[float]:
    return [start + i * 0.1 for i in range(n)]


class _Stub(BaseAgent):
    id = "stub"
    version = "1"

    def analyze(self, ctx: AnalysisContext) -> AgentSignal:
        return self.build_signal(
            ctx,
            direction=Direction.FLAT,
            confidence=0.0,
            rationale="stub",
            features={},
        )


@pytest.mark.unit
def test_signal_validates_confidence_bounds() -> None:
    base = dict(
        agent_id="technical",
        version="1",
        symbol="EURUSD",
        timeframe="M15",
        direction=Direction.LONG,
        bucket_ts=datetime(2026, 8, 21, 10, 0, tzinfo=UTC),
        created_at=datetime(2026, 8, 21, 10, 0, tzinfo=UTC),
        valid_until=datetime(2026, 8, 21, 10, 30, tzinfo=UTC),
    )
    with pytest.raises(ValidationError):
        AgentSignal(**base, confidence=1.5, rationale="", features={})
    with pytest.raises(ValidationError):
        AgentSignal(**base, confidence=-0.1, rationale="", features={})


@pytest.mark.unit
def test_signal_rejects_unknown_timeframe() -> None:
    with pytest.raises(ValidationError):
        AgentSignal(
            agent_id="x",
            version="1",
            symbol="EURUSD",
            timeframe="W1",
            direction=Direction.FLAT,
            confidence=0.0,
            bucket_ts=datetime.now(UTC),
            created_at=datetime.now(UTC),
            valid_until=datetime.now(UTC),
        )


@pytest.mark.unit
def test_freshness_boundaries() -> None:
    bucket = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    expiry = bucket + timedelta(minutes=30)

    def make(now: datetime) -> AgentSignal:
        return AgentSignal(
            agent_id="technical",
            version="1",
            symbol="EURUSD",
            timeframe="M15",
            direction=Direction.LONG,
            confidence=0.4,
            bucket_ts=bucket,
            rationale="",
            features={},
            created_at=bucket,
            valid_until=expiry,
        )

    assert make(bucket).is_fresh(bucket)
    # Valid-until is inclusive: fresh up to and including expiry, stale after.
    assert make(expiry - timedelta(seconds=1)).is_fresh(expiry - timedelta(seconds=1))
    assert make(expiry).is_fresh(expiry)
    assert not make(expiry).is_fresh(expiry + timedelta(seconds=1))


@pytest.mark.unit
def test_freshness_policy_per_agent_family() -> None:
    m15_seconds = 900
    bucket = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    # Technical expires after 2 intervals, regime after 4.
    assert vf(agent_id="technical", bucket_ts=bucket, timeframe="M15") == (
        bucket + timedelta(seconds=m15_seconds * 2)
    )
    assert vf(agent_id="regime", bucket_ts=bucket, timeframe="M15") == (
        bucket + timedelta(seconds=m15_seconds * 4)
    )
    # Unknown agents fall back to 2x.
    assert vf(agent_id="mystery", bucket_ts=bucket, timeframe="H1") == (
        bucket + timedelta(seconds=3600 * 2)
    )


@pytest.mark.unit
def test_registry_duplicate_and_lookup() -> None:
    registry = AgentRegistry()
    stub_a, stub_b = _Stub(), _Stub()
    registry.register(stub_a)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(stub_b)
    assert registry.get("stub") is stub_a
    assert registry.get("missing") is None
    assert len(registry) == 1
    with pytest.raises(ValueError, match="non-empty"):

        class _NoId(BaseAgent):  # type: ignore[type-arg]
            id = ""
            version = "1"

            def analyze(self, ctx: AnalysisContext) -> AgentSignal:
                raise AssertionError

        registry.register(_NoId())


@pytest.mark.unit
def test_base_agent_is_abstract() -> None:
    with pytest.raises(TypeError):
        BaseAgent()  # type: ignore[abstract]


@pytest.mark.unit
def test_context_holds_inputs() -> None:
    ctx = _ctx()
    assert isinstance(ctx.candles, pd.DataFrame)
    assert set(ctx.meta) == set()
    assert ctx.symbol == "EURUSD"
