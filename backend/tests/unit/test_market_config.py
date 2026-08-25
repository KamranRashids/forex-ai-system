"""Unit tests: runtime market-config normalization (pure validators)."""

from __future__ import annotations

import pytest
from app.data.market_config import normalize_symbols, normalize_timeframes


@pytest.mark.unit
def test_normalize_symbols_uppercases_dedupes() -> None:
    assert normalize_symbols(["eurusd", "EURUSD", " usdjpy "]) == ["EURUSD", "USDJPY"]


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["EUR", "NOTAPAIR", "eur$usd", ""])
def test_normalize_symbols_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError, match="Invalid FX pair"):
        normalize_symbols([bad])


@pytest.mark.unit
def test_normalize_symbols_requires_at_least_one() -> None:
    with pytest.raises(ValueError, match="At least one pair"):
        normalize_symbols([])


@pytest.mark.unit
def test_normalize_timeframes_sorts_by_rank_and_dedupes() -> None:
    assert normalize_timeframes(["D1", "m5", "H4"]) == ["M5", "H4", "D1"]
    assert normalize_timeframes(["h1", "H1"]) == ["H1"]


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["W1", "M1", "yearly"])
def test_normalize_timeframes_rejects_unknown(bad: str) -> None:
    with pytest.raises(ValueError, match="Unknown timeframe"):
        normalize_timeframes([bad])


@pytest.mark.unit
def test_normalize_timeframes_requires_at_least_one() -> None:
    with pytest.raises(ValueError, match="At least one timeframe"):
        normalize_timeframes([])
