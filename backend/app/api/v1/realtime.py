"""Realtime: WebSocket ticket issuance + the live stream endpoint (Phase 8).

Auth flow (decision #6): an authenticated REST call to ``POST /api/v1/ws/ticket``
issues a short-lived, one-time ticket. The client then opens
``/api/v1/ws/stream?ticket=<ticket>``. No long-lived JWT/access token ever
appears in a URL or query string.

The socket is read/observe only (SAFE MODE): it forwards durable Stream events
(alerts / signals / decisions) to connections that subscribed to an
RBAC-approved topic. It cannot place orders or alter trading state.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from redis.asyncio import Redis

from app.alerts.translate import digest_event_id
from app.api.deps import CurrentUser
from app.core.config import get_settings
from app.core.constants import API_V1_PREFIX
from app.core.errors import AuthenticationError
from app.core.metrics import (
    WS_CONNECTIONS,
    WS_CONNECTIONS_TOTAL,
    WS_ERROR_TOTAL,
    WS_MESSAGES_TOTAL,
)
from app.db.session import get_redis
from app.ws.hub import (
    ALLOWED_TOPICS,
    TOPIC_STREAMS,
    WS_AUTH_CLOSE_CODE,
    current_stream_id,
    origin_allowed,
    parse_subscribe,
    read_once,
)
from app.ws.tickets import TICKET_TTL_SECONDS, check_topic_access, issue_ticket, redeem_ticket

logger = structlog.stdlib.get_logger(__name__)

router = APIRouter(prefix="/ws", tags=["realtime"])

RedisDep = Annotated[Redis, Depends(get_redis)]

_POLL_TIMEOUT: float = 0.1


class TicketOut(BaseModel):
    ticket: str
    ws_url: str
    expires_in: int


def _ws_path(ticket: str) -> str:
    return f"{API_V1_PREFIX}/ws/stream?ticket={ticket}"


@router.post("/ticket")
async def create_ws_ticket(user: CurrentUser, redis: RedisDep) -> TicketOut:
    """Issue a short-lived, one-time WebSocket ticket for the caller."""
    payload = await issue_ticket(
        redis,
        user_id=user.id,
        role=user.role.value,
        ttl_seconds=TICKET_TTL_SECONDS,
    )
    return TicketOut(
        ticket=payload.ticket, ws_url=_ws_path(payload.ticket), expires_in=payload.ttl_seconds
    )


def _event_frame(topic: str, event: Any) -> dict[str, Any]:
    """Build a server->client frame for one event.

    The frame's ``data`` is the event payload enriched with ``event_type``,
    ``produced_at`` and the canonical durable ``event_id`` (from the same digest
    the alerts worker uses when persisting ``alert_events``), so clients can key
    live events identically to the REST ``AlertOut.event_id`` without risking
    collisions between distinct events.
    """
    payload = dict(getattr(event, "payload", {}) or {})
    payload.setdefault("event_type", getattr(event, "event_type", "unknown"))
    payload.setdefault("produced_at", getattr(event, "produced_at", None))
    payload.setdefault("event_id", digest_event_id(event))
    if hasattr(event, "produced_at") and event.produced_at is not None:
        with suppress(Exception):  # noqa: BLE001 - keep the frame robust
            payload["produced_at"] = event.produced_at.isoformat()
    return {"type": "event", "topic": topic, "data": payload}


@router.websocket("/stream")
async def ws_stream(
    websocket: WebSocket,
    redis: RedisDep,
) -> None:
    """Live observer stream. Requires a one-time ticket from POST /ws/ticket."""
    origin = websocket.headers.get("origin")
    if not origin_allowed(origin, get_settings().cors_origin_list):
        await websocket.close(code=WS_AUTH_CLOSE_CODE)
        return

    ticket = websocket.query_params.get("ticket", "")
    try:
        ident = await redeem_ticket(redis, ticket)
    except AuthenticationError:
        await websocket.close(code=WS_AUTH_CLOSE_CODE)
        return

    await websocket.accept()
    WS_CONNECTIONS.inc()
    WS_CONNECTIONS_TOTAL.inc()

    topics: set[str] = set()
    #: Per-topic last-read cursor. A topic is baselined once, at the moment it
    #: is first subscribed, to the current head of its Stream so only events
    #: produced *after* subscription are delivered (initial data comes from the
    #: REST endpoints, per decision #5).
    since: dict[str, str] = {}
    try:
        while True:
            if topics:
                for topic in topics:
                    if topic not in since:
                        since[topic] = await current_stream_id(redis, TOPIC_STREAMS[topic])
                entries_by_topic, since = await read_once(redis, since, topics)
                for topic, entries in entries_by_topic.items():
                    for _entry_id, event in entries:
                        if event is None:
                            continue
                        WS_MESSAGES_TOTAL.labels(topic=topic).inc()
                        await websocket.send_json(_event_frame(topic, event))

            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=_POLL_TIMEOUT)
            except TimeoutError:
                continue
            except WebSocketDisconnect:
                break

            msg_type = message.get("type")
            if msg_type == "websocket.disconnect":
                break
            if msg_type == "websocket.receive":
                text = message.get("text")
                if text:
                    error = _handle_control(text, ident.role, topics)
                    if error:
                        WS_ERROR_TOTAL.labels(reason="control").inc()
                        await websocket.send_json({"type": "error", "reason": error})
                    else:
                        await websocket.send_json({"type": "subscribed", "topics": sorted(topics)})
    finally:
        WS_CONNECTIONS.dec()
        with suppress(Exception):  # noqa: BLE001 - already closed
            await websocket.close()


def _handle_control(text: str, role: str, topics: set[str]) -> str | None:
    """Apply an inbound control frame; returns an error string or None.

    Topic isolation: only RBAC-approved, phase-allowed topics are added; any
    reserved/unknown topic (e.g. "fills") is refused and reported.
    """
    parsed = parse_subscribe(text)
    if isinstance(parsed, str):
        return parsed

    requested = [str(t).lower() for t in parsed.get("topics", [])]
    refused: list[str] = []
    for topic in requested:
        if topic not in ALLOWED_TOPICS or not check_topic_access(role, topic):
            refused.append(topic)
            continue
        topics.add(topic)
    if refused:
        return f"topics not permitted: {sorted(refused)}"
    if not requested:
        return "subscribe requires at least one topic"
    return None
