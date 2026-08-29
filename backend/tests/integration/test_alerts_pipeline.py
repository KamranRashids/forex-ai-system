"""Integration tests: alert pipeline (stream -> worker -> REST), ticket + WS.

Covers Phase 8 end-to-end with real PostgreSQL (alert_events) + fake Redis:
- the alerts worker persists ``alerts.stream`` entries idempotently,
- REST list is viewer+ (401 unauth; 200 authed),
- ack is admin-only (403 for viewer/trader),
- ticket issuance is authenticated and one-time,
- the WebSocket stream requires a valid ticket and delivers live alerts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from app.bus.events import Event
from app.bus.topics import ALERTS_STREAM
from fastapi.testclient import TestClient


def _alert_event() -> Event:
    return Event(
        event_type="alert.staleness",
        payload={
            "source": "monitor",
            "symbol": "EURUSD",
            "timeframe": "M15",
            "age_seconds": 3600,
            "threshold_seconds": 2700,
        },
        producer="monitor",
        produced_at=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )


async def _seed_alert(fake_redis: Any) -> None:
    from app.bus.topics import ALERTS_STREAM as STREAM

    await fake_redis.xadd(STREAM, {"data": _alert_event().to_json()})


async def _run_worker_once(db_sessionmaker: Any, fake_redis: Any) -> None:
    from app.workers.alert_worker import AlertWorker

    worker = AlertWorker(session_factory=db_sessionmaker, redis=fake_redis)
    await worker.ensure_group()
    await worker.poll_once()


async def _tokens(client: Any) -> dict[str, str]:
    from tests.integration.conftest import bearer, register_and_login

    body = await register_and_login(client, "alerts@example.com")
    return bearer(body["access_token"])


@pytest.mark.asyncio
async def test_worker_persists_durably_and_rest_lists(
    client: Any, db_sessionmaker: Any, fake_redis: Any
) -> None:
    # Redelivery (two stream entries with the SAME event) collapses into one row.
    await fake_redis.xadd(ALERTS_STREAM, {"data": _alert_event().to_json()})
    await fake_redis.xadd(ALERTS_STREAM, {"data": _alert_event().to_json()})
    await _run_worker_once(db_sessionmaker, fake_redis)

    events = await fake_redis.xrange(ALERTS_STREAM)
    assert len(events) == 2

    headers = await _tokens(client)
    resp = await client.get("/api/v1/alerts", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1  # idempotent: two entries, one persisted row
    item = body["items"][0]
    assert item["event_type"] == "alert.staleness"
    assert item["symbol"] == "EURUSD"
    assert item["severity"] == "warning"
    assert item["acknowledged_at"] is None


@pytest.mark.asyncio
async def test_alerts_list_requires_auth(
    client: Any, db_sessionmaker: Any, fake_redis: Any
) -> None:
    resp = await client.get("/api/v1/alerts")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ack_is_admin_only(client: Any, db_sessionmaker: Any, fake_redis: Any) -> None:
    from tests.integration.conftest import bearer, register_and_login

    await _seed_alert(fake_redis)
    await _run_worker_once(db_sessionmaker, fake_redis)

    # First registered user is auto-promoted to admin (bootstrap).
    admin = await register_and_login(client, "admin@example.com")
    admin_headers = bearer(admin["access_token"])

    resp = await client.get("/api/v1/alerts", headers=admin_headers)
    event_id = resp.json()["items"][0]["event_id"]

    # Non-admin (second user, defaults to viewer) cannot ack.
    party = await register_and_login(client, "viewer@example.com")
    denied = await client.post(
        f"/api/v1/alerts/{event_id}/ack", headers=bearer(party["access_token"])
    )
    assert denied.status_code == 403
    # Admin can ack.
    ok = await client.post(f"/api/v1/alerts/{event_id}/ack", headers=admin_headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["acknowledged_at"] is not None
    assert ok.json()["acknowledged_by"] == "admin@example.com"


@pytest.mark.asyncio
async def test_ws_ticket_issued_to_authenticated_user(client: Any, fake_redis: Any) -> None:
    from tests.integration.conftest import bearer, register_and_login

    body = await register_and_login(client, "ws@example.com")
    resp = await client.post("/api/v1/ws/ticket", headers=bearer(body["access_token"]))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ticket"]
    assert data["expires_in"] == 30

    # Unauthenticated cannot issue a ticket.
    unauth = await client.post("/api/v1/ws/ticket")
    assert unauth.status_code == 401


@pytest.mark.asyncio
async def test_ws_stream_requires_ticket_and_delivers_alert(
    db_sessionmaker: Any, fake_redis: Any
) -> None:
    from app.core.config import reset_settings_cache
    from app.db.session import get_db, get_redis
    from app.main import create_app

    reset_settings_cache()
    application = create_app()

    async def override_get_db():
        session = db_sessionmaker()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def override_get_redis():
        yield fake_redis

    application.dependency_overrides[get_db] = override_get_db
    application.dependency_overrides[get_redis] = override_get_redis

    # Issue a ticket via the app.
    from app.ws.tickets import issue_ticket

    ticket_payload = await issue_ticket(
        fake_redis, user_id=__import__("uuid").uuid4(), role="viewer"
    )

    with (
        TestClient(application) as test_client,
        test_client.websocket_connect(
            f"/api/v1/ws/stream?ticket={ticket_payload.ticket}"
        ) as socket,
    ):
        socket.send_json({"type": "subscribe", "topics": ["alerts"]})
        ack = socket.receive_json()
        assert ack["type"] == "subscribed"
        assert "alerts" in ack["topics"]

        from datetime import UTC, datetime

        event = Event(
            event_type="alert.risk_brake",
            payload={"source": "risk", "symbol": "GBPUSD", "veto_code": "drawdown"},
            producer="orchestrator",
            produced_at=datetime.now(UTC),
        )
        await fake_redis.xadd(ALERTS_STREAM, {"data": event.to_json()})
        frame = socket.receive_json()
        assert frame["type"] == "event"
        assert frame["topic"] == "alerts"
        assert frame["data"]["symbol"] == "GBPUSD"
        # The live identity equals the durable persisted identity.
        from app.alerts.translate import digest_event_id

        assert frame["data"]["event_id"] == digest_event_id(event)


@pytest.mark.asyncio
async def test_ws_stream_rejects_missing_ticket(fake_redis: Any) -> None:
    from app.core.config import reset_settings_cache
    from app.db.session import get_redis
    from app.main import create_app
    from app.ws.hub import WS_AUTH_CLOSE_CODE
    from starlette.websockets import WebSocketDisconnect

    reset_settings_cache()
    application = create_app()

    async def override_get_redis():
        yield fake_redis

    application.dependency_overrides[get_redis] = override_get_redis

    with (
        TestClient(application) as test_client,
        pytest.raises(WebSocketDisconnect) as exc_info,
    ):
        with test_client.websocket_connect("/api/v1/ws/stream?ticket=bogus") as _socket:
            _socket.receive_text()
        assert exc_info.value.code == WS_AUTH_CLOSE_CODE


@pytest.mark.asyncio
async def test_ws_stream_allows_configured_origin(fake_redis: Any) -> None:
    from app.core.config import reset_settings_cache
    from app.db.session import get_redis
    from app.main import create_app
    from app.ws.tickets import issue_ticket

    reset_settings_cache()
    application = create_app()

    async def override_get_redis():
        yield fake_redis

    application.dependency_overrides[get_redis] = override_get_redis

    ticket = await issue_ticket(fake_redis, user_id=__import__("uuid").uuid4(), role="viewer")

    with (
        TestClient(application) as test_client,
        test_client.websocket_connect(
            f"/api/v1/ws/stream?ticket={ticket.ticket}",
            headers={"Origin": "http://localhost:3000"},
        ) as socket,
    ):
        socket.send_json({"type": "subscribe", "topics": ["alerts"]})
        ack = socket.receive_json()
        assert ack["type"] == "subscribed"


@pytest.mark.asyncio
async def test_ws_stream_rejects_disallowed_origin_with_valid_ticket(fake_redis: Any) -> None:
    from app.core.config import reset_settings_cache
    from app.db.session import get_redis
    from app.main import create_app
    from app.ws.hub import WS_AUTH_CLOSE_CODE
    from app.ws.tickets import issue_ticket
    from starlette.websockets import WebSocketDisconnect

    reset_settings_cache()
    application = create_app()

    async def override_get_redis():
        yield fake_redis

    application.dependency_overrides[get_redis] = override_get_redis

    # A valid ticket is NOT enough: a cross-site origin is blocked (CSWSH).
    ticket = await issue_ticket(fake_redis, user_id=__import__("uuid").uuid4(), role="viewer")

    with (
        TestClient(application) as test_client,
        pytest.raises(WebSocketDisconnect) as exc_info,
    ):
        with test_client.websocket_connect(
            f"/api/v1/ws/stream?ticket={ticket.ticket}",
            headers={"Origin": "http://evil.example"},
        ) as _socket:
            _socket.receive_text()
        assert exc_info.value.code == WS_AUTH_CLOSE_CODE
