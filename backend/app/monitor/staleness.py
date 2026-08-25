"""Staleness watchdogs (monitor component).

A series is stale when its newest stored closed bar is older than three
timeframe intervals (floor of 15 minutes) *while the FX market is open*.
Breaches are published as ``alert.staleness`` events on ``events.alerts``
and logged; UI surfacing arrives with the WebSocket hub (Phase 8+).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bus.events import Event
from app.bus.publisher import EventPublisher, NullEventPublisher
from app.data.market_hours import is_market_open
from app.data.repository import last_closed_ts
from app.data.timeframes import Timeframe
from app.models.instrument import Instrument

logger = structlog.stdlib.get_logger(__name__)

_MIN_THRESHOLD_SECONDS: int = 900


def staleness_threshold_seconds(timeframe: str) -> int:
    """Alert once a series lags more than 3 bars behind real time."""
    return max(Timeframe.seconds(timeframe) * 3, _MIN_THRESHOLD_SECONDS)


@dataclass(frozen=True, slots=True)
class StalenessFinding:
    symbol: str
    timeframe: str
    age_seconds: int | None  # None => no bars at all
    threshold_seconds: int
    market_open: bool

    @property
    def breached(self) -> bool:
        if not self.market_open:
            return False
        if self.age_seconds is None:
            return True
        return self.age_seconds > self.threshold_seconds


class StalenessMonitor:
    """Evaluates series freshness and emits alerts on breach."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: EventPublisher | None = None,
    ) -> None:
        self._sessions = session_factory
        self._publisher = publisher or NullEventPublisher()

    async def check(
        self,
        instruments: list[Instrument],
        timeframes: list[str],
        *,
        now: datetime,
    ) -> list[StalenessFinding]:
        findings: list[StalenessFinding] = []
        market_open = is_market_open(now)
        async with self._sessions() as session:
            for instrument in instruments:
                for timeframe in timeframes:
                    last_ts = await last_closed_ts(
                        session, instrument_id=instrument.id, timeframe=timeframe
                    )
                    age = int((now - last_ts).total_seconds()) if last_ts is not None else None
                    finding = StalenessFinding(
                        symbol=instrument.symbol,
                        timeframe=timeframe,
                        age_seconds=age,
                        threshold_seconds=staleness_threshold_seconds(timeframe),
                        market_open=market_open,
                    )
                    findings.append(finding)
                    if finding.breached:
                        await self._emit_alert(finding, now)
        return findings

    async def _emit_alert(self, finding: StalenessFinding, now: datetime) -> None:
        logger.warning(
            "staleness_breach",
            symbol=finding.symbol,
            timeframe=finding.timeframe,
            age_seconds=finding.age_seconds,
            threshold_seconds=finding.threshold_seconds,
            market_open=finding.market_open,
        )
        event = Event(
            event_type="alert.staleness",
            payload={
                "symbol": finding.symbol,
                "timeframe": finding.timeframe,
                "age_seconds": finding.age_seconds,
                "threshold_seconds": finding.threshold_seconds,
                "market_open": finding.market_open,
            },
            producer="monitor",
            produced_at=now,
        )
        try:
            await self._publisher.publish_alert(event)
        except Exception:  # noqa: BLE001 - alerting must never crash monitors
            logger.debug("alert_publish_failed", symbol=finding.symbol)
