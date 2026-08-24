"""Integration: refresh rotation, expiry, and reuse detection (family revocation)."""

from __future__ import annotations

import httpx
import pytest
from tests.integration.conftest import register_and_login

pytestmark = [pytest.mark.integration]


@pytest.mark.asyncio
async def test_refresh_rotates_and_old_token_dies(client: httpx.AsyncClient) -> None:
    await register_and_login(client, "rotate@example.com")

    first = await client.post("/api/v1/auth/refresh")
    assert first.status_code == 200
    old_cookie = client.cookies.get("refresh_token")

    second = await client.post("/api/v1/auth/refresh")
    assert second.status_code == 200
    new_cookie = client.cookies.get("refresh_token")
    assert new_cookie != old_cookie


@pytest.mark.asyncio
async def test_replayed_refresh_revokes_whole_family(client: httpx.AsyncClient) -> None:
    """Presenting a superseded token must kill every token in the family."""
    await register_and_login(client, "reuse@example.com")

    first = await client.post("/api/v1/auth/refresh")
    stolen_cookie = client.cookies.get("refresh_token")
    assert first.status_code == 200

    # Legitimate client rotates again; the earlier cookie is now stale.
    second = await client.post("/api/v1/auth/refresh")
    assert second.status_code == 200
    latest_cookie = client.cookies.get("refresh_token")

    # Attacker replays the stolen (already-rotated) token.
    client.cookies.set("refresh_token", stolen_cookie)
    replay = await client.post("/api/v1/auth/refresh")
    assert replay.status_code == 401
    assert "reuse" in replay.json()["detail"].lower()

    # Even the newest legitimate token is dead — family revoked.
    client.cookies.set("refresh_token", latest_cookie)
    after_revoke = await client.post("/api/v1/auth/refresh")
    assert after_revoke.status_code == 401


@pytest.mark.asyncio
async def test_expired_refresh_rejected(client: httpx.AsyncClient) -> None:
    import freezegun

    await register_and_login(client, "expiry@example.com")
    raw_cookie = client.cookies.get("refresh_token")
    assert raw_cookie

    # Send the token via explicit header: the httpx cookie jar applies its own
    # (real-clock) expiry semantics, which would drop it under freeze_time.
    client.cookies.clear()
    # Refresh lifetime is 14 days; jump well past it.
    with freezegun.freeze_time("2100-01-01T00:00:00Z"):
        resp = await client.post(
            "/api/v1/auth/refresh", headers={"Cookie": f"refresh_token={raw_cookie}"}
        )
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_refresh_issues_working_access_token(client: httpx.AsyncClient) -> None:
    await register_and_login(client, "usable@example.com")

    refreshed = await client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200
    new_tokens = refreshed.json()

    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {new_tokens['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == "usable@example.com"
