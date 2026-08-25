"""Integration: admin market-config API + backfill queueing (RBAC enforced)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from app.models.audit_log import AuditLog
from tests.integration.conftest import bearer, register_and_login

pytestmark = [pytest.mark.integration]

PASSWORD = "correct-horse-battery-staple"


async def _admin_headers(client: httpx.AsyncClient) -> dict[str, str]:
    tokens = await register_and_login(client, "phase2-admin@example.com", PASSWORD)
    return bearer(tokens["access_token"])


@pytest.mark.asyncio
async def test_market_config_defaults_match_settings(client: httpx.AsyncClient) -> None:
    headers = await _admin_headers(client)
    resp = await client.get("/api/v1/admin/market-config", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "synthetic"
    assert body["symbols"][0] == "EURUSD"
    assert body["timeframes"] == ["M15", "H1", "H4"]  # ADR-0003 defaults (test env)
    assert set(body["supported_timeframes"]) == {"M5", "M15", "H1", "H4", "D1"}


@pytest.mark.asyncio
async def test_market_config_update_persists_override(
    client: httpx.AsyncClient, db_sessionmaker: Any
) -> None:
    from app.models.system_setting import SystemSetting
    from sqlalchemy import select

    headers = await _admin_headers(client)
    put = await client.put(
        "/api/v1/admin/market-config",
        json={"timeframes": ["H4", "M5", "D1"], "symbols": ["EURUSD", "USDJPY"]},
        headers=headers,
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["timeframes"] == ["M5", "H4", "D1"]  # normalized ascending by rank
    assert body["symbols"] == ["EURUSD", "USDJPY"]

    async with db_sessionmaker() as session:
        row = await session.get(SystemSetting, "market.timeframes")
        assert row is not None and row.value["value"] == ["M5", "H4", "D1"]
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "admin.market_config_updated")
                )
            )
            .scalars()
            .all()
        )
        assert audits, "expected audit entry for config update"


@pytest.mark.asyncio
async def test_market_config_validation_rejects_bad_input(
    client: httpx.AsyncClient,
) -> None:
    headers = await _admin_headers(client)
    bad_tf = await client.put(
        "/api/v1/admin/market-config", json={"timeframes": ["W1"]}, headers=headers
    )
    assert bad_tf.status_code in (400, 422)

    bad_pair = await client.put(
        "/api/v1/admin/market-config", json={"symbols": ["NOTAPAIR"]}, headers=headers
    )
    assert bad_pair.status_code in (400, 422)


@pytest.mark.asyncio
async def test_backfill_queues_job_for_worker(client: httpx.AsyncClient, fake_redis: Any) -> None:
    headers = await _admin_headers(client)
    start = datetime.now(UTC) - timedelta(days=7)
    end = datetime.now(UTC)
    resp = await client.post(
        "/api/v1/admin/backfill",
        json={
            "symbols": ["EURUSD"],
            "timeframes": ["M15"],
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["queued_jobs"] == 1

    raw = await fake_redis.rpop("jobs:backfill")
    assert raw is not None
    job = json.loads(raw)
    assert job["symbols"] == ["EURUSD"]
    assert job["timeframes"] == ["M15"]
    assert datetime.fromisoformat(job["end"]) > datetime.fromisoformat(job["start"])


@pytest.mark.asyncio
async def test_backfill_rejects_inverted_or_huge_ranges(client: httpx.AsyncClient) -> None:
    headers = await _admin_headers(client)
    now = datetime.now(UTC)

    inverted = await client.post(
        "/api/v1/admin/backfill",
        json={
            "symbols": ["EURUSD"],
            "start": now.isoformat(),
            "end": (now - timedelta(days=1)).isoformat(),
        },
        headers=headers,
    )
    assert inverted.status_code == 500 or inverted.status_code in (400, 422)

    huge = await client.post(
        "/api/v1/admin/backfill",
        json={
            "symbols": ["EURUSD"],
            "start": (now - timedelta(days=200)).isoformat(),
            "end": now.isoformat(),
        },
        headers=headers,
    )
    assert huge.status_code in (400, 422)


@pytest.mark.asyncio
async def test_admin_routes_require_admin_role(client: httpx.AsyncClient) -> None:
    # Anonymous
    anon = await client.get("/api/v1/admin/market-config")
    assert anon.status_code == 401

    # Non-admin user (first registered is admin; second is viewer by default)
    await register_and_login(client, "the-admin@example.com", PASSWORD)
    viewer_tokens = await register_and_login(client, "a-viewer@example.com", PASSWORD)
    viewer = bearer(viewer_tokens["access_token"])

    forbidden = await client.get("/api/v1/admin/market-config", headers=viewer)
    assert forbidden.status_code == 403

    put_forbidden = await client.put(
        "/api/v1/admin/market-config",
        json={"timeframes": ["D1"]},
        headers=viewer,
    )
    assert put_forbidden.status_code == 403
