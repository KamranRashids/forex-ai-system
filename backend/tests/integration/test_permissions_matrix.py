"""Integration: RBAC permission matrix over the admin users API."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.integration.conftest import bearer, register_and_login

pytestmark = [pytest.mark.integration]

PASSWORD = "correct-horse-battery-staple"


async def _create_user_with_role(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    email: str,
    role: str,
) -> dict[str, str]:
    """Register via API then set role directly; returns auth headers."""
    tokens = await register_and_login(client, email, PASSWORD)
    from app.models.user import User
    from sqlalchemy import update

    async with db_sessionmaker() as session:
        await session.execute(update(User).where(User.email == email).values(role=role))
        await session.commit()
    return bearer(tokens["access_token"])


@pytest.mark.asyncio
async def test_anonymous_gets_401(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/users")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_viewer_blocked_from_user_list(
    client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await register_and_login(client, "admin-seed@example.com", PASSWORD)
    viewer = await _create_user_with_role(client, db_sessionmaker, "viewer@example.com", "viewer")

    resp = await client.get("/api/v1/users", headers=viewer)
    assert resp.status_code == 403
    assert resp.json()["type"].endswith("/problems/forbidden")


@pytest.mark.asyncio
async def test_trader_blocked_from_admin_routes(
    client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await register_and_login(client, "admin-seed@example.com", PASSWORD)
    trader = await _create_user_with_role(client, db_sessionmaker, "trader@example.com", "trader")

    listing = await client.get("/api/v1/users", headers=trader)
    assert listing.status_code == 403

    count = await client.get("/api/v1/users/count", headers=trader)
    assert count.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_list_users(
    client: httpx.AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    admin_tokens = await register_and_login(client, "boss@example.com", PASSWORD)  # first -> admin
    await _create_user_with_role(client, db_sessionmaker, "peer@example.com", "trader")

    resp = await client.get("/api/v1/users", headers=bearer(admin_tokens["access_token"]))
    assert resp.status_code == 200
    emails = {u["email"] for u in resp.json()}
    assert {"boss@example.com", "peer@example.com"} <= emails


@pytest.mark.asyncio
async def test_admin_promotes_viewer_to_trader(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    admin_tokens = await register_and_login(client, "promoter@example.com", PASSWORD)
    admin_headers = bearer(admin_tokens["access_token"])
    viewer_headers = await _create_user_with_role(
        client, db_sessionmaker, "rising-star@example.com", "viewer"
    )

    users = await client.get("/api/v1/users", headers=admin_headers)
    target_id = next(u["id"] for u in users.json() if u["email"] == "rising-star@example.com")

    patch = await client.patch(
        f"/api/v1/users/{target_id}", json={"role": "trader"}, headers=admin_headers
    )
    assert patch.status_code == 200
    assert patch.json()["role"] == "trader"

    me = await client.get("/api/v1/auth/me", headers=viewer_headers)
    assert me.status_code == 200


@pytest.mark.asyncio
async def test_trader_cannot_administer_users(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await register_and_login(client, "seed-admin@example.com", PASSWORD)
    trader = await _create_user_with_role(
        client, db_sessionmaker, "just-trader@example.com", "trader"
    )

    users_as_admin = await client.get("/api/v1/users")  # anonymous fallback check
    assert users_as_admin.status_code == 401

    resp = await client.patch(
        "/api/v1/users/00000000-0000-0000-0000-000000000000",
        json={"is_active": False},
        headers=trader,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_cannot_change_own_role(
    client: httpx.AsyncClient,
) -> None:
    tokens = await register_and_login(client, "self-guard@example.com", PASSWORD)
    headers = bearer(tokens["access_token"])

    me = await client.get("/api/v1/auth/me", headers=headers)
    self_id = me.json()["id"]

    resp = await client.patch(f"/api/v1/users/{self_id}", json={"role": "viewer"}, headers=headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_deactivation_audited(
    client: httpx.AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from app.models.audit_log import AuditLog
    from sqlalchemy import select

    admin_tokens = await register_and_login(client, "auditor@example.com", PASSWORD)
    admin_headers = bearer(admin_tokens["access_token"])
    await _create_user_with_role(client, db_sessionmaker, "target@example.com", "viewer")

    users = await client.get("/api/v1/users", headers=admin_headers)
    target_id = next(u["id"] for u in users.json() if u["email"] == "target@example.com")

    patch = await client.patch(
        f"/api/v1/users/{target_id}", json={"is_active": False}, headers=admin_headers
    )
    assert patch.status_code == 200
    assert patch.json()["is_active"] is False

    async with db_sessionmaker() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.action == "admin.user_updated")))
            .scalars()
            .all()
        )
        assert rows, "expected an audit entry for the role update"
        entry = rows[-1]
        assert entry.after is not None and entry.after.get("is_active") is False
