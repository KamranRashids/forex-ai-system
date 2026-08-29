"""Worker heartbeats (monitor component, Phase 7).

Each runtime worker refreshes a short-TTL Redis hash keyed by role so the API
(``/system/status`` and ``/metrics``) can report per-process liveness without a
dedicated monitor worker. A worker is considered:

- ``up``   when its heartbeat key exists and was refreshed recently;
- ``stale`` when the key exists but is older than the heartbeat TTL (process
  hung or its loop stopped touching it);
- ``down`` when the key is absent (process exited without clearing it, or the
  TTL fully expired).

Heartbeats are observational only — they never gate trading and never touch
any order path (SAFE MODE preserved).
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from app.bus.topics import worker_heartbeat_key

#: Default TTL applied to heartbeat keys (sec). TTL auto-expiry is what lets a
#: killed process be detected as ``down`` without needing a cleanup step.
DEFAULT_HEARTBEAT_TTL_SECONDS: int = 60

#: Roles we expect to observe. Kept in sync with worker_main.py dispatch.
WORKER_ROLES: tuple[str, ...] = ("ingest", "agents", "content", "orchestrator")

_LAST_SEEN_FIELD: str = "last_seen"
_STARTED_AT_FIELD: str = "started_at"
_PID_FIELD: str = "pid"
_TTL_FIELD: str = "ttl_seconds"


@dataclass(frozen=True, slots=True)
class WorkerHealth:
    """Live liveness picture of a single worker role."""

    role: str
    status: str  # "up" | "stale" | "down"
    last_seen: datetime | None
    started_at: datetime | None
    age_seconds: float | None
    ttl_seconds: int


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _ts_now(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(UTC).isoformat()


class WorkerHeartbeat:
    """Refreshes a worker's heartbeat hash in Redis until stopped."""

    def __init__(
        self,
        redis: object,
        role: str,
        *,
        ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
        started_at: datetime | None = None,
        pid: int | None = None,
    ) -> None:
        self._redis = redis
        self._role = role
        self._ttl = max(1, int(ttl_seconds))
        self._started_at = started_at or datetime.now(UTC)
        self._pid = pid or _current_pid()
        self._key = worker_heartbeat_key(role)
        self._last_seen: datetime | None = None

    @property
    def key(self) -> str:
        return self._key

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    async def touch(self, *, now: datetime | None = None) -> None:
        """Refresh the heartbeat (call once per loop iteration)."""
        timestamp = now or datetime.now(UTC)
        await self._redis.hset(  # type: ignore[attr-defined]
            self._key,
            mapping={
                _LAST_SEEN_FIELD: _ts_now(timestamp),
                _STARTED_AT_FIELD: _ts_now(self._started_at),
                _PID_FIELD: str(self._pid),
                _TTL_FIELD: str(self._ttl),
            },
        )
        await self._redis.expire(self._key, self._ttl)  # type: ignore[attr-defined]
        self._last_seen = timestamp

    async def clear(self) -> None:
        """Remove the heartbeat on graceful shutdown (marks the role ``down``)."""
        with suppress(Exception):  # noqa: BLE001 - shutdown cleanup must never raise
            await self._redis.delete(self._key)  # type: ignore[attr-defined]


def _current_pid() -> int:
    import os

    return os.getpid()


async def read_worker_health(
    redis: object,
    *,
    roles: tuple[str, ...] = WORKER_ROLES,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
) -> list[WorkerHealth]:
    """Read liveness for every role by inspecting its heartbeat key.

    The classification window comes from the worker's *declared* TTL (stored in
    the hash), which each worker sets to comfortably exceed its loop cadence, so
    healthy slow-loop workers are never misclassified as stale/down. When a hash
    holds no TTL field, the caller-supplied ``ttl_seconds`` is the fallback.
    """
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    health: list[WorkerHealth] = []
    for role in roles:
        key = worker_heartbeat_key(role)
        raw = await redis.hgetall(key)  # type: ignore[attr-defined]
        raw_dict = raw if isinstance(raw, dict) else {}
        last_seen = _parse_ts(raw_dict.get(_LAST_SEEN_FIELD))
        started_at = _parse_ts(raw_dict.get(_STARTED_AT_FIELD))
        try:
            declared_ttl = int(raw_dict.get(_TTL_FIELD, ttl_seconds))
        except (TypeError, ValueError):
            declared_ttl = ttl_seconds
        effective_ttl = max(1, declared_ttl)
        age = (timestamp - last_seen).total_seconds() if last_seen is not None else None
        status = heartbeat_status(age, effective_ttl)
        health.append(
            WorkerHealth(
                role=role,
                status=status,
                last_seen=last_seen,
                started_at=started_at,
                age_seconds=age,
                ttl_seconds=effective_ttl,
            )
        )
    return health


def heartbeat_status(age_seconds: float | None, ttl_seconds: int) -> str:
    """Classify a single heartbeat age into ``up`` / ``stale`` / ``down``."""
    if age_seconds is None:
        return "down"
    if age_seconds > ttl_seconds:
        return "stale"
    return "up"


def heartbeat_ttl_for_loop(
    loop_period_seconds: int, *, min_ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS
) -> int:
    """Pick a heartbeat TTL that comfortably exceeds a worker's loop cadence.

    A healthy worker is "up" for the whole interval between its refreshes, so
    the TTL must exceed the loop period by a safety margin. The default margin
    is 3x the loop period, floored at ``min_ttl_seconds``.
    """
    return max(int(min_ttl_seconds), max(1, int(loop_period_seconds)) * 3)
