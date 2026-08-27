"""Redis-backed event publisher (Streams for durable facts, Pub/Sub for fan-out)."""

from __future__ import annotations

from typing import Protocol

from redis.asyncio import Redis

from app.bus.events import Event
from app.bus.topics import (
    DECISIONS_STREAM,
    EVENTS_ALERTS_CHANNEL,
    PRICES_LIVE_CHANNEL,
    SIGNALS_STREAM,
    STREAM_MAXLEN_APPROX,
    bars_closed_topic,
    latest_price_key,
)


class EventPublisher(Protocol):
    async def publish_bar_closed(self, event: Event, *, timeframe: str) -> None: ...

    async def publish_price(self, event: Event) -> None: ...

    async def publish_alert(self, event: Event) -> None: ...

    async def publish_signal(self, event: Event) -> None: ...

    async def publish_decision(self, event: Event) -> None: ...

    async def set_latest_price(self, symbol: str, quote_json: str) -> None: ...


class RedisEventPublisher:
    """Publishes to ``bars.closed.{tf}`` streams and JSON Pub/Sub channels."""

    def __init__(self, redis: Redis, producer_name: str = "ingest") -> None:
        self._redis = redis
        self._producer = producer_name

    async def publish_bar_closed(self, event: Event, *, timeframe: str) -> None:
        await self._redis.xadd(
            bars_closed_topic(timeframe),
            {"data": event.to_json()},
            maxlen=STREAM_MAXLEN_APPROX,
            approximate=True,
        )

    async def publish_price(self, event: Event) -> None:
        await self._redis.publish(PRICES_LIVE_CHANNEL, event.to_json())

    async def publish_alert(self, event: Event) -> None:
        await self._redis.publish(EVENTS_ALERTS_CHANNEL, event.to_json())

    async def publish_signal(self, event: Event) -> None:
        await self._redis.xadd(
            SIGNALS_STREAM,
            {"data": event.to_json()},
            maxlen=STREAM_MAXLEN_APPROX,
            approximate=True,
        )

    async def publish_decision(self, event: Event) -> None:
        await self._redis.xadd(
            DECISIONS_STREAM,
            {"data": event.to_json()},
            maxlen=STREAM_MAXLEN_APPROX,
            approximate=True,
        )

    async def set_latest_price(self, symbol: str, quote_json: str) -> None:
        await self._redis.set(latest_price_key(symbol), quote_json)


class NullEventPublisher:
    """No-op publisher (used when Redis is unavailable or in dry runs)."""

    async def publish_bar_closed(self, event: Event, *, timeframe: str) -> None:
        return None

    async def publish_price(self, event: Event) -> None:
        return None

    async def publish_alert(self, event: Event) -> None:
        return None

    async def publish_signal(self, event: Event) -> None:
        return None

    async def publish_decision(self, event: Event) -> None:
        return None

    async def set_latest_price(self, symbol: str, quote_json: str) -> None:
        return None
