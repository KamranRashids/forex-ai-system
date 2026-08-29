"""Unit tests: WebSocket Origin allow-list hardening (Phase 8 security)."""

from __future__ import annotations

import pytest
from app.ws.hub import origin_allowed

_ALLOWED: list[str] = ["http://localhost:3000", "https://app.example.com"]


@pytest.mark.unit
def test_origin_allowed_matches_exact_allow_list() -> None:
    assert origin_allowed("http://localhost:3000", _ALLOWED) is True
    assert origin_allowed("https://app.example.com", _ALLOWED) is True


@pytest.mark.unit
def test_origin_allowed_rejects_disallowed_origins() -> None:
    assert origin_allowed("http://evil.example", _ALLOWED) is False
    assert origin_allowed("https://app.example.com.evil.io", _ALLOWED) is False
    assert origin_allowed("http://localhost:9999", _ALLOWED) is False
    # Scheme mismatch is not allowed even on the same host.
    assert origin_allowed("https://localhost:3000", _ALLOWED) is False


@pytest.mark.unit
def test_origin_allowed_tolerates_missing_or_empty() -> None:
    # Non-browser/replay clients may omit Origin; the ticket still gates access.
    assert origin_allowed(None, _ALLOWED) is True
    assert origin_allowed("", _ALLOWED) is True
    assert origin_allowed("null", _ALLOWED) is False


@pytest.mark.unit
def test_origin_allowed_with_empty_allow_list() -> None:
    # An empty allow-list is the most restrictive: any present Origin is blocked.
    assert origin_allowed("http://localhost:3000", []) is False
    assert origin_allowed(None, []) is True
