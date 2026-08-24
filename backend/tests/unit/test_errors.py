"""Unit tests: problem+json serialization for every error class."""

from __future__ import annotations

from typing import Any

import pytest
from app.core.errors import (
    AppError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    register_exception_handlers,
)
from fastapi import FastAPI


def _build_app() -> FastAPI:
    from asgi_correlation_id import CorrelationIdMiddleware

    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    @app.get("/not-found")
    async def not_found() -> None:
        raise NotFoundError("Widget missing")

    @app.get("/forbidden")
    async def forbidden() -> None:
        raise PermissionDeniedError("Nope")

    @app.get("/limited")
    async def limited() -> None:
        raise RateLimitError(42)

    @app.get("/http-exc")
    async def http_exc() -> None:
        from starlette.exceptions import HTTPException

        raise HTTPException(status_code=418, detail="teapot")

    @app.post("/items")
    async def items(body: dict[str, int]) -> dict[str, int]:  # pragma: no cover
        return body

    return app


@pytest.fixture()
def client() -> Any:
    from fastapi.testclient import TestClient

    return TestClient(_build_app(), raise_server_exceptions=False)


@pytest.mark.unit
def test_app_error_shape_is_problem_json(client: Any) -> None:
    resp = client.get("/not-found")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == 404
    assert body["title"] == "Resource not found"
    assert body["detail"] == "Widget missing"
    assert body["instance"] == "/not-found"
    assert body["type"].endswith("/problems/not_found")
    assert resp.headers["x-request-id"]


@pytest.mark.unit
def test_permission_denied_problem(client: Any) -> None:
    resp = client.get("/forbidden")
    assert resp.status_code == 403
    body = resp.json()
    assert body["title"] == "Permission denied"
    assert body["detail"] == "Nope"


@pytest.mark.unit
def test_rate_limit_problem_includes_retry_after(client: Any) -> None:
    resp = client.get("/limited")
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "42"
    assert resp.json()["retry_after"] == 42


@pytest.mark.unit
def test_starlette_http_exception_becomes_problem(client: Any) -> None:
    resp = client.get("/http-exc")
    assert resp.status_code == 418
    body = resp.json()
    assert body["detail"] == "teapot"
    assert body["type"].endswith("http_418")


@pytest.mark.unit
def test_unhandled_exception_returns_sanitized_500(client: Any) -> None:
    resp = client.get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert "kaboom" not in (body["detail"] or "")
    assert body["correlation_id"] or resp.headers.get("x-request-id")


@pytest.mark.unit
def test_validation_error_is_problem_with_errors_list(client: Any) -> None:
    resp = client.post("/items", json={"not": "an int dict"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["type"].endswith("validation_error")
    assert isinstance(body["errors"], list) and body["errors"]


@pytest.mark.unit
def test_rate_limiter_error_defaults() -> None:
    err = RateLimitError(7)
    assert err.extras == {"retry_after": 7}
    assert AppError().status_code == 500
