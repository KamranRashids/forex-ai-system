"""Unit tests: decision input-hashing (audit/replay determinism)."""

from __future__ import annotations

import pytest
from app.decisions.hashing import inputs_hash


@pytest.mark.unit
def test_hash_deterministic() -> None:
    a = inputs_hash(
        symbol="eurusd",
        timeframe="h1",
        bucket_ts="2026-01-01T12:00:00+00:00",
        agent_versions={"technical": "1", "sentiment": "2"},
        weights={"technical": 0.55, "sentiment": 0.25},
    )
    b = inputs_hash(
        symbol="EURUSD",
        timeframe="H1",
        bucket_ts="2026-01-01T12:00:00+00:00",
        agent_versions={"technical": "1", "sentiment": "2"},
        weights={"sentiment": 0.25, "technical": 0.55},
    )
    assert a == b
    assert len(a) == 64


@pytest.mark.unit
def test_hash_differs_on_bucket() -> None:
    a = inputs_hash(
        symbol="EURUSD",
        timeframe="H1",
        bucket_ts="2026-01-01T12:00:00+00:00",
        agent_versions={"technical": "1"},
        weights={"technical": 1.0},
    )
    b = inputs_hash(
        symbol="EURUSD",
        timeframe="H1",
        bucket_ts="2026-01-01T13:00:00+00:00",
        agent_versions={"technical": "1"},
        weights={"technical": 1.0},
    )
    assert a != b


@pytest.mark.unit
def test_hash_differs_on_version() -> None:
    a = inputs_hash(
        symbol="EURUSD",
        timeframe="H1",
        bucket_ts="x",
        agent_versions={"technical": "1"},
        weights={"technical": 1.0},
    )
    b = inputs_hash(
        symbol="EURUSD",
        timeframe="H1",
        bucket_ts="x",
        agent_versions={"technical": "2"},
        weights={"technical": 1.0},
    )
    assert a != b


@pytest.mark.unit
def test_hash_differs_on_symbol_and_timeframe() -> None:
    base = dict(agent_versions={"technical": "1"}, weights={"technical": 1.0})
    assert inputs_hash(symbol="EURUSD", timeframe="H1", bucket_ts="x", **base) != inputs_hash(
        symbol="USDJPY", timeframe="H1", bucket_ts="x", **base
    )
    assert inputs_hash(symbol="EURUSD", timeframe="H1", bucket_ts="x", **base) != inputs_hash(
        symbol="EURUSD", timeframe="H4", bucket_ts="x", **base
    )


@pytest.mark.unit
def test_hash_weights_floating_rounding_stable() -> None:
    a = inputs_hash(
        symbol="EURUSD",
        timeframe="H1",
        bucket_ts="x",
        agent_versions={"t": "1"},
        weights={"t": 0.3333333333},
    )
    b = inputs_hash(
        symbol="EURUSD",
        timeframe="H1",
        bucket_ts="x",
        agent_versions={"t": "1"},
        weights={"t": 0.3333333339},
    )
    assert a == b
