"""FX market-hours calendar (UTC).

v1 model: the interbank FX week opens Sunday 22:00 UTC and closes Friday
22:00 UTC, with no intraday breaks. This is a simplification (broker hours
vary by minutes); refined in Phase 7 with DST-safe calendars.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

#: weekday() numbering: Mon=0 ... Sun=6
_OPEN_SUNDAY_HOUR: int = 22
_CLOSE_FRIDAY_HOUR: int = 22


def is_market_open(ts: datetime) -> bool:
    """True when the FX week is open at ``ts`` (UTC)."""
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    utc = ts.astimezone(UTC)
    weekday = utc.weekday()

    if weekday == 6:  # Sunday: closed until 22:00
        return utc.hour >= _OPEN_SUNDAY_HOUR
    if weekday == 4:  # Friday: open until 22:00
        return utc.hour < _CLOSE_FRIDAY_HOUR
    return weekday != 5  # Saturday closed; Monday-Thursday open


def _next_boundary(*, weekday: int, hour: int, minute: int, after: datetime) -> datetime:
    """Next datetime strictly after ``after`` at the given weekday/hour/minute (UTC)."""
    candidate = after.astimezone(UTC).replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (weekday - candidate.weekday()) % 7
    if days_ahead == 0 and candidate <= after:
        days_ahead = 7
    elif days_ahead != 0 and candidate < after:
        pass  # candidate already later today? handled by <= above when same day
    if days_ahead > 0:
        candidate += timedelta(days=days_ahead)
    while candidate <= after:  # defensive loop (never iterates given logic above)
        candidate += timedelta(days=7)
    return candidate


def next_open(ts: datetime) -> datetime:
    """The first instant at/after ``ts`` when the market is open."""
    utc = ts.astimezone(UTC)
    sunday_open = _next_boundary(
        weekday=6, hour=_OPEN_SUNDAY_HOUR, minute=0, after=utc - timedelta(seconds=1)
    )
    monday_start = _next_boundary(weekday=0, hour=0, minute=0, after=utc - timedelta(seconds=1))
    candidates = [sunday_open]
    if is_market_open(utc):
        return utc
    candidates.append(monday_start)
    return min(candidates)


def next_close(ts: datetime) -> datetime:
    """The first instant strictly after ``ts`` when the market closes."""
    utc = ts.astimezone(UTC)
    friday_close = _next_boundary(weekday=4, hour=_CLOSE_FRIDAY_HOUR, minute=0, after=utc)
    if is_market_open(utc):
        return friday_close
    # Market closed: the next close follows the next opening.
    opening = next_open(utc)
    return _next_boundary(weekday=4, hour=_CLOSE_FRIDAY_HOUR, minute=0, after=opening)
