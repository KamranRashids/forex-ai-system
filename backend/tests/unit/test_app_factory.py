"""Smoke tests for the application factory (no external services required)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


@pytest.mark.unit
def test_root_reports_metadata(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "forex-ai-api"
    assert body["mode"] == "safe"
    assert body["docs"] == "/docs"


@pytest.mark.safety
@pytest.mark.unit
def test_health_live_reports_safe_mode(client: TestClient) -> None:
    resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "mode": "safe"}


@pytest.mark.unit
def test_openapi_documents_phase1_surface(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    for path in (
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        "/api/v1/auth/logout",
        "/api/v1/auth/me",
        "/health/live",
        "/system/status",
    ):
        assert path in paths, f"missing {path}"
    assert schema["info"]["title"] == "Forex AI System API"


@pytest.mark.safety
@pytest.mark.unit
def test_openapi_carries_safe_mode_notice(client: TestClient) -> None:
    description = client.get("/openapi.json").json()["info"]["description"]
    assert "PAPER TRADING ONLY" in description


@pytest.mark.unit
def test_correlation_id_header_returned(client: TestClient) -> None:
    resp = client.get("/health/live")
    assert resp.headers.get("x-request-id")


@pytest.mark.unit
def test_metrics_endpoint_serves_prometheus_text(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.text.startswith("#")
