"""Unit tests: short-lived one-time WebSocket tickets (Phase 8, decision #6)."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from app.core.errors import AuthenticationError
from app.ws.tickets import (
    TICKET_TTL_SECONDS,
    check_topic_access,
    issue_ticket,
    redeem_ticket,
)
from fakeredis.aioredis import FakeRedis


@pytest_asyncio.fixture()
async def redis():
    client = FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.mark.asyncio
async def test_issue_and_redeem_roundtrip(redis: FakeRedis) -> None:
    uid = uuid.uuid4()
    issued = await issue_ticket(redis, user_id=uid, role="viewer")
    assert issued.ticket
    assert issued.role == "viewer"
    assert issued.sub == str(uid)
    assert issued.ttl_seconds == TICKET_TTL_SECONDS

    redeemed = await redeem_ticket(redis, issued.ticket)
    assert redeemed.sub == str(uid)
    assert redeemed.role == "viewer"


@pytest.mark.asyncio
async def test_ticket_is_single_use(redis: FakeRedis) -> None:
    issued = await issue_ticket(redis, user_id=uuid.uuid4(), role="trader")
    await redeem_ticket(redis, issued.ticket)
    with pytest.raises(AuthenticationError):
        await redeem_ticket(redis, issued.ticket)


@pytest.mark.asyncio
async def test_missing_or_bogus_ticket_rejected(redis: FakeRedis) -> None:
    with pytest.raises(AuthenticationError):
        await redeem_ticket(redis, "")
    with pytest.raises(AuthenticationError):
        await redeem_ticket(redis, "definitely-not-a-ticket")


@pytest.mark.asyncio
async def test_ticket_expires(redis: FakeRedis) -> None:
    issued = await issue_ticket(redis, user_id=uuid.uuid4(), role="viewer", ttl_seconds=1)
    # Simulate the key expiring (Redis drops it after its TTL).
    keys = list(await redis.keys("ws:ticket:*"))
    assert len(keys) == 1
    await redis.delete(keys[0])
    with pytest.raises(AuthenticationError):
        await redeem_ticket(redis, issued.ticket)


def test_topic_rbac_gating() -> None:
    assert check_topic_access("viewer", "alerts") is True
    assert check_topic_access("viewer", "signals") is True
    assert check_topic_access("viewer", "decisions") is True
    # Reserved/unknown topics (fills) are not subscribable in Phase 8.
    assert check_topic_access("admin", "fills") is False
    assert check_topic_access("admin", "garbage") is False
