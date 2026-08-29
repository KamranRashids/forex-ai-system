"""Short-lived, one-time WebSocket tickets (Phase 8, decision #6).

Rationale: long-lived JWT/access tokens must NEVER appear in a WebSocket URL or
query string (they leak via logs/history). Instead, an authenticated REST call
issues a short-lived, single-use ticket bound to (user, role, allowed topics)
and stored in Redis with a TTL. The client opens the socket with ``?ticket=...``
and the server validates + consumes it in one atomic step, then binds the
connection to the resolved identity and role.

SAFE MODE: this only authenticates a read/observe stream. It never authorizes
any control or order action.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.errors import AuthenticationError
from redis.asyncio import Redis

#: TTL for an unused ticket (seconds). Short-lived by design.
TICKET_TTL_SECONDS: int = 30

#: Topic -> minimum role required to subscribe (viewer < trader < admin).
_TOPIC_MIN_ROLE: dict[str, str] = {
    "alerts": "viewer",
    "signals": "viewer",
    "decisions": "viewer",
}

_TICKET_KEY_PREFIX: str = "ws:ticket:"


def role_rank(role: str) -> int:
    from app.core.security import ROLE_RANK

    return ROLE_RANK.get(role, -1)


def check_topic_access(role: str, topic: str) -> bool:
    """True when ``role`` may subscribe to ``topic`` (RBAC + topic isolation)."""
    required = _TOPIC_MIN_ROLE.get(topic)
    if required is None:
        # Unknown/reserved (e.g. "fills") are never subscribable in this phase.
        return False
    return role_rank(role) >= role_rank(required)


def _key(ticket: str) -> str:
    return f"{_TICKET_KEY_PREFIX}{ticket}"


@dataclass(frozen=True, slots=True)
class TicketPayload:
    ticket: str
    sub: str
    role: str
    ttl_seconds: int
    issued_at: datetime


async def issue_ticket(
    redis: Redis,
    *,
    user_id: uuid.UUID,
    role: str,
    ttl_seconds: int = TICKET_TTL_SECONDS,
) -> TicketPayload:
    """Create a one-time ticket; consumes a Redis key that the socket redeems."""
    ticket = secrets.token_urlsafe(24)
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": now.isoformat(),
    }
    await redis.set(_key(ticket), __import__("json").dumps(payload), ex=ttl_seconds)
    return TicketPayload(
        ticket=ticket, sub=str(user_id), role=role, ttl_seconds=ttl_seconds, issued_at=now
    )


async def redeem_ticket(redis: Redis, ticket: str) -> TicketPayload:
    """Atomically validate + consume a ticket.

    Returns the resolved identity, or raises :class:`AuthenticationError` when
    the ticket is missing/expired/already used. One-time use is guaranteed by
    `GETDEL`: a concurrent second redemption gets nothing.
    """
    if not ticket:
        raise AuthenticationError("Missing WebSocket ticket")

    raw = await redis.getdel(_key(ticket))
    if not raw:
        raise AuthenticationError("Invalid, expired, or already-used WebSocket ticket")

    import json

    try:
        payload = json.loads(raw)
        role = str(payload.get("role", ""))
        sub = str(payload.get("sub", ""))
    except (ValueError, TypeError) as exc:  # noqa: BLE001 - malformed stored ticket
        raise AuthenticationError("Malformed WebSocket ticket") from exc

    from app.core.security import ROLE_RANK

    if not sub or role not in ROLE_RANK:
        raise AuthenticationError("Malformed WebSocket ticket")

    return TicketPayload(
        ticket=ticket, sub=sub, role=role, ttl_seconds=0, issued_at=datetime.now(UTC)
    )
