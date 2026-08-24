"""Smoke tests for the Phase 0 placeholder API."""

from __future__ import annotations

import pytest
from app.main import APP_NAME, app, validate_safe_mode
from fastapi.testclient import TestClient


@pytest.mark.unit
def test_health_live_reports_safe_mode() -> None:
    client = TestClient(app)
    resp = client.get("/health/live")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mode"] == "safe"


@pytest.mark.unit
def test_root_reports_metadata() -> None:
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["name"] == APP_NAME


@pytest.mark.safety
@pytest.mark.parametrize("bad_mode", ["live", "real", "paper", "", None])
def test_validate_safe_mode_rejects_everything_else(bad_mode: str | None) -> None:
    with pytest.raises(RuntimeError, match="Refusing to start"):
        validate_safe_mode(bad_mode)


@pytest.mark.safety
def test_validate_safe_mode_is_case_insensitive() -> None:
    assert validate_safe_mode(" SAFE ") == "safe"
