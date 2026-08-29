"""WebSocket realtime hub helpers (Phase 8, decision #7).

The realtime feed is an *observer* front-end over the durable Redis Streams
(``alerts.stream``, ``signals.stream``, ``decisions.stream``). Connections are
authenticated by a one-time ticket (see :mod:`app.ws.tickets`) and subscribe to
a topic set. A connection's reader tails its subscribed streams with
non-destructive ``XREAD`` (no consumer group — observing/forwarding never
steals events from the orchestrator/alert workers that own the groups).

Topics:
- ``alerts``    — alert events (durable records persisted by the alerts worker).
- ``signals``   — agent signal triggers.
- ``decisions`` — orchestrator decision outputs.
- ``fills``     — RESERVED for a later phase; not subscribable in Phase 8.

SAFE MODE: every frame is read-observation only. No frame can place an order,
mutate trading state, or trigger any control action.
"""

from __future__ import annotations

from typing import Any

from app.bus.events import Event
from app.bus.topics import ALERTS_STREAM, DECISIONS_STREAM, SIGNALS_STREAM

#: topic -> durable stream the reader tails for that topic.
TOPIC_STREAMS: dict[str, str] = {
    "alerts": ALERTS_STREAM,
    "signals": SIGNALS_STREAM,
    "decisions": DECISIONS_STREAM,
}

#: Topics that may be subscribed in this phase ("fills" is reserved).
ALLOWED_TOPICS: frozenset[str] = frozenset(TOPIC_STREAMS)

WS_AUTH_CLOSE_CODE: int = 4401
WS_INVALID_TOPIC_CLOSE_CODE: int = 4403

#: Max inbound control frame size (bytes) — bounds abuse.
_MAX_CONTROL_BYTES: int = 4096


def parse_subscribe(text: str) -> dict[str, Any] | str:
    """Parse an inbound control frame into a dict, or an error string.

    Accepted protocol: ``{"type":"subscribe","topics":["alerts", ...]}``.
    Unknown types / malformed JSON return an error string.
    """
    if not text or len(text.encode("utf-8")) > _MAX_CONTROL_BYTES:
        return "control frame too large"
    import json

    try:
        msg = json.loads(text)
    except (ValueError, TypeError):
        return "malformed JSON"
    if not isinstance(msg, dict):
        return "control frame must be an object"
    if msg.get("type") != "subscribe":
        return "unsupported control type"
    topics = msg.get("topics")
    if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
        return "topics must be a list of strings"
    return msg


def _decode_field(fields: dict[str, Any], key: str) -> str:
    raw = fields.get(key)
    if isinstance(raw, bytes):
        return raw.decode()
    return str(raw or "")


def decode_stream_entries(entries: list[Any]) -> list[tuple[str, Event | None]]:
    """Decode (stream_id, Event) pairs from an XREAD response entry list.

    Malformed/poison entries yield ``None`` events but are still returned with
    their id so the caller can advance its cursor (never retry-poison-forever).
    """
    out: list[tuple[str, Event | None]] = []
    for entry_id, fields in entries:
        raw = _decode_field(fields, "data")
        event: Event | None = None
        if raw:
            try:
                event = Event.from_json(raw)
            except Exception:  # noqa: BLE001 - poison entry tolerated
                event = None
        out.append((str(entry_id), event))
    return out


def topic_for_stream(stream_name: str) -> str | None:
    """Map a stream name back to its topic, or None."""
    for topic, stream in TOPIC_STREAMS.items():
        if stream == stream_name:
            return topic
    return None


def origin_allowed(origin: str | None, allowed: list[str]) -> bool:
    """True when a WebSocket ``Origin`` header is permitted.

    Defense-in-depth against Cross-Site WebSocket Hijacking (CSWSH): browsers
    always send an ``Origin`` on WebSocket upgrades, so a malicious site's
    browser cannot reach us unless that origin is explicitly allowed. A missing
    or empty ``Origin`` (e.g. non-browser CLI/replay clients) is permitted —
    such connections are still authenticated by the mandatory one-time ticket.
    Origins are matched exactly against the configured allow-list (the same
    ``cors_origins`` values used for the HTTP CORS middleware).
    """
    if not origin:
        return True
    return origin in allowed


async def current_stream_id(redis: Any, stream: str) -> str:
    """Latest entry id of a stream, or ``"0-0"`` when empty.

    Used to baseline a connection's cursor at subscribe time so it only sees
    events produced *after* subscribing (initial data comes from REST, per
    decision #5). ``$`` cannot be used with non-blocking XREAD as it never
    returns in-flight entries.
    """
    try:
        newest = await redis.xrevrange(stream, max="+", min="-", count=1)
    except Exception:  # noqa: BLE001 - bus outage must not crash WebSockets
        return "0-0"
    if not newest:
        return "0-0"
    return str(newest[0][0])


async def read_once(
    redis: Any, since: dict[str, str], topics: set[str]
) -> tuple[dict[str, list[tuple[str, Event | None]]], dict[str, str]]:
    """Non-destructive XREAD of new entries for the subscribed topics.

    Returns ``(entries_by_topic, new_since)``. ``since`` maps topic -> last
    stream id; each subscribed topic is baselined to its current head at
    subscribe time (see :func:`current_stream_id`). The cursor advances past
    every read entry (including poison) so a malformed event is skipped exactly
    once and never blocks the feed.
    """
    if not topics:
        return {}, dict(since)
    streams: dict[str, str] = {TOPIC_STREAMS[t]: since.get(t, "0-0") for t in topics}
    try:
        response = await redis.xread(streams, count=50)
    except Exception:  # noqa: BLE001 - bus outages must not crash WebSockets
        return {}, dict(since)

    entries_by_topic: dict[str, list[tuple[str, Event | None]]] = {}
    new_since = dict(since)
    for stream_name, entries in response or []:
        topic = topic_for_stream(str(stream_name))
        if topic is None:
            continue
        decoded = decode_stream_entries(entries)
        entries_by_topic[topic] = decoded
        if decoded:
            new_since[topic] = decoded[-1][0]
    return entries_by_topic, new_since
