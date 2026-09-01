"""Unit tests: ``prices.latest`` cache payload parsing (Phase 12 O-1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from app.api.v1.market import _parse_price


def _entry(**overrides: object) -> str:
    payload: dict[str, object] = {
        "symbol": "EURUSD",
        "price": 1.0856,
        "bucket_start": "2026-08-21T12:45:00.000000Z",
        "synthetic": True,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parses_valid_entry() -> None:
    quote = _parse_price("EURUSD", _entry())
    assert quote is not None
    assert quote.symbol == "EURUSD"
    assert quote.price == Decimal("1.0856")
    assert quote.bucket_start == datetime(2026, 8, 21, 12, 45, tzinfo=UTC)
    assert quote.synthetic is True


def test_returns_none_for_invalid_json() -> None:
    assert _parse_price("EURUSD", "not-json") is None


def test_returns_none_for_non_dict_json() -> None:
    assert _parse_price("EURUSD", json.dumps([1, 2, 3])) is None


def test_returns_none_when_price_missing() -> None:
    assert _parse_price("EURUSD", _entry(price=None)) is None


def test_returns_none_when_price_unparseable() -> None:
    assert _parse_price("EURUSD", _entry(price="abc")) is None


def test_tolerates_unparseable_bucket_start() -> None:
    quote = _parse_price("EURUSD", _entry(bucket_start="not-a-time"))
    assert quote is not None
    assert quote.bucket_start is None


def test_defaults_synthetic_to_false() -> None:
    quote = _parse_price("EURUSD", _entry(synthetic=False))
    assert quote is not None
    assert quote.synthetic is False
