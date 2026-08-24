"""Integration: full auth flow over the real API + migrated database."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from tests.integration.conftest import bearer, register_and_login

pytestmark = [pytest.mark.integration]


@pytest.mark.asyncio
async def test_register_login_me_happy_path(client: httpx.AsyncClient) -> None:
    reg = await client.post(
        "/api/v1/auth/register",
        json={"email": "Owner@Example.com", "password": "correct-horse-battery-staple"},
    )
    assert reg.status_code == 201
    created = reg.json()
    assert created["email"] == "owner@example.com"  # normalized
    assert created["role"] == "admin"  # first user bootstraps as admin

    login = await client.post(
        "/api/v1/auth/login",
        data={"username": "owner@example.com", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200
    token_body = login.json()
    assert token_body["token_type"] == "bearer"
    assert "expires_at" in token_body

    me = await client.get("/api/v1/auth/me", headers=bearer(token_body["access_token"]))
    assert me.status_code == 200
    assert me.json()["email"] == "owner@example.com"
    assert me.json()["role"] == "admin"
    assert me.json()["last_login_at"] is not None


@pytest.mark.asyncio
async def test_duplicate_registration_conflicts(client: httpx.AsyncClient) -> None:
    body = {"email": "dup@example.com", "password": "correct-horse-battery-staple"}
    first = await client.post("/api/v1/auth/register", json=body)
    second = await client.post("/api/v1/auth/register", json=body)
    assert first.status_code == 201
    assert second.status_code == 409
    problem = second.json()
    assert problem["type"].endswith("/problems/conflict")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": "correct-horse-battery-staple"},
        {"email": "short@example.com", "password": "short"},
        {"password": "correct-horse-battery-staple"},
    ],
)
async def test_register_validation_rejected(
    client: httpx.AsyncClient, payload: dict[str, str]
) -> None:
    resp = await client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["type"].endswith("validation_error")
    assert isinstance(body["errors"], list)


@pytest.mark.asyncio
async def test_second_user_defaults_to_viewer(client: httpx.AsyncClient) -> None:
    await register_and_login(client, "first-admin@example.com")
    second = await client.post(
        "/api/v1/auth/register",
        json={"email": "second@example.com", "password": "correct-horse-battery-staple"},
    )
    assert second.status_code == 201
    assert second.json()["role"] == "viewer"


@pytest.mark.asyncio
async def test_wrong_password_401_problem_shape(client: httpx.AsyncClient) -> None:
    await client.post(
        "/api/v1/auth/register",
        json={"email": "user@example.com", "password": "correct-horse-battery-staple"},
    )
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": "user@example.com", "password": "totally-wrong-password"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["type"].endswith("/problems/unauthorized")
    assert body["detail"] == "Incorrect email or password"


@pytest.mark.asyncio
async def test_unknown_email_same_error_as_wrong_password(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": "ghost@example.com", "password": "correct-horse-battery-staple"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"


@pytest.mark.asyncio
async def test_inactive_user_cannot_login_or_use_token(
    client: httpx.AsyncClient, db_sessionmaker: Any
) -> None:
    tokens = await register_and_login(client, "inactive@example.com")
    headers = bearer(tokens["access_token"])

    async with db_sessionmaker() as session:
        from app.models.user import User
        from sqlalchemy import update

        await session.execute(
            update(User).where(User.email == "inactive@example.com").values(is_active=False)
        )
        await session.commit()

    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 401

    login = await client.post(
        "/api/v1/auth/login",
        data={"username": "inactive@example.com", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 401


@pytest.mark.asyncio
async def test_me_requires_token(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.json()["title"] == "Authentication required"


@pytest.mark.asyncio
async def test_logout_requires_authentication(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/logout")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_refresh_cookie(client: httpx.AsyncClient) -> None:
    tokens = await register_and_login(client, "logout@example.com")
    auth = bearer(tokens["access_token"])

    refreshed = await client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200

    logout_ok = await client.post("/api/v1/auth/logout", headers=auth)
    assert logout_ok.status_code == 200

    after_logout = await client.post("/api/v1/auth/refresh")
    assert after_logout.status_code == 401


@pytest.mark.asyncio
async def test_refresh_without_cookie_is_unauthorized(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/refresh")
    assert resp.status_code == 401
    assert "Missing refresh" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_rate_limit_blocks_login_flood(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import reset_settings_cache

    monkeypatch.setenv("RATE_LIMIT_LOGIN_PER_MINUTE", "3")
    reset_settings_cache()

    await register_and_login(client, "limited@example.com")
    statuses = []
    for _ in range(5):
        resp = await client.post(
            "/api/v1/auth/login",
            data={"username": "limited@example.com", "password": "wrong-password-xxxxx"},
        )
        statuses.append(resp.status_code)
        if resp.status_code == 429:
            break
    assert 429 in statuses
    reset_settings_cache()
