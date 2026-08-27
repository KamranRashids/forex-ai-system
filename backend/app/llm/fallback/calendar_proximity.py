"""Economic-calendar proximity impact model (deterministic fallback).

Maps how close a high-impact event is to a reference instant into:
- ``impact`` in [0, 1] — peak inside the event window, decaying outside;
- ``window`` — minutes before/after the event during which impact is non-zero;
- ``reason`` — a human-readable, deterministic rationale string.

Used by the fundamental agent to express event-window risk states without an
LLM (implementation requirement: zero-key operation stays deterministic).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

#: Half-window (minutes each side) of elevated risk around an event.
_WINDOW_MINUTES: dict[str, int] = {"low": 0, "medium": 45, "high": 90}


@dataclass(frozen=True, slots=True)
class EventImpact:
    """Deterministic proximity-based impact of a single event."""

    impact: float  # [0, 1]
    window_minutes: int
    within_window: bool
    reason: str


def event_impact(
    event_ts: datetime,
    now: datetime,
    importance: str,
    *,
    half_window_minutes: int | None = None,
) -> EventImpact | None:
    """Impact of an ``importance`` event occurring at ``event_ts`` for ``now``.

    Returns ``None`` when the event is far enough away that it has no bearing
    (or importance is ``low``). The peak is at ``event_ts`` and decays linearly
    over the configured window on either side.
    """
    half = (
        half_window_minutes
        if half_window_minutes is not None
        else _WINDOW_MINUTES.get(importance, 0)
    )
    if half <= 0:
        return None
    delta = abs((now - event_ts).total_seconds()) / 60.0
    if delta > half:
        return None
    impact = max(0.0, 1.0 - delta / half)
    within = delta <= half
    return EventImpact(
        impact=round(impact, 4),
        window_minutes=half,
        within_window=within,
        reason=(
            f"{importance} impact event '{event_ts.isoformat()}' "
            f"{delta:.0f}min from reference; impact {impact:.2f}"
        ),
    )


def proximity_bucket(events: list[tuple[datetime, str]], now: datetime) -> dict[str, object]:
    """Aggregate the nearest event-driven impact across a set of (ts, importance).

    Returns a summary dict with the max impact, the nearest impacted event time,
    and the window width, or empty-safe defaults when no event is near.
    """
    best: tuple[float, datetime, str] | None = None
    for event_ts, importance in events:
        impact = event_impact(event_ts, now, importance)
        if impact is None:
            continue
        delta = abs((now - event_ts).total_seconds())
        if best is None:
            best = (impact.impact, event_ts, importance)
            continue
        best_delta = abs((now - best[1]).total_seconds())
        if impact.impact > best[0] or (impact.impact == best[0] and delta < best_delta):
            best = (impact.impact, event_ts, importance)
    if best is None:
        return {"impact": 0.0, "nearest_ts": None, "importance": None, "window_minutes": 0}
    best_impact, event_ts, importance = best
    return {
        "impact": best_impact,
        "nearest_ts": event_ts,
        "importance": importance,
        "window_minutes": _WINDOW_MINUTES.get(importance, 0) * 2,
    }


def event_window(importance: str, now: datetime, event_ts: datetime) -> timedelta | None:
    """Remaining window as a duration, or None when not inside one."""
    imp = event_impact(event_ts, now, importance)
    if imp is None:
        return None
    return timedelta(minutes=imp.window_minutes)
