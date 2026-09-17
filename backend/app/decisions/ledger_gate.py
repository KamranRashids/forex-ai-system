"""Ledger-derived risk gate for live paper decisions (Phase 14A).

The live orchestrator's risk gates are computed from the actual paper ledger —
the post-``restore_state()`` ``LedgerBroker`` plus the persisted OPEN / CLOSED
position rows — so they answer the same question in the same units as the
backtest driver's gates:

- ``exposure_pct``     = Σ(OPEN units x entry_price) / equity
- ``correlation_pct``  = Σ(OPEN LONG basket notional) / equity
- ``daily_loss_pct``   = max(0, -Σ CLOSED net_pnl today) / equity
- ``drawdown_pct``     = broker's tracked max drawdown from peak

Only OPEN positions count toward exposure (backtest parity); PENDING unfilled
orders do not consume capital and are excluded behind an explicit view-model
constant the reviewer can flip with a one-line change.

The built result is a :class:`LedgerGateView`: the ``GateState`` consumed by
``assess`` plus the raw ledger projections the engine's reporting mirror and
the ``gate_snapshot`` observability line need.

SAFE MODE: this module is a read-only paper projection. It imports no execution
or routing surface and can never place or time a real order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import structlog
from app.agents.base import Direction
from app.broker.ledger import LedgerBroker
from app.broker.positions import Position, PositionSet
from app.core.metrics import PAPER_GATE_USED_PCT
from app.decisions.risk import GateState
from app.models.paper_ledger import PaperPositionRow

logger = structlog.stdlib.get_logger(__name__)

#: View-model: do PENDING (unfilled) orders count toward exposure? ``False``
#: keeps backtest parity — the driver counts only filled/OPEN positions.
#: Flip to ``True`` for a conservative view that also counts unfilled intent.
INCLUDE_PENDING_IN_EXPOSURE: Final[bool] = False

#: Cancellation-reason label for the PENDING-expiry sweep (log/metric surface
#: only — there is deliberately no ``cancel_reason`` column).
CANCEL_EXPIRED_PENDING: Final[str] = "expired_pending"

_EPOCH: datetime = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class LedgerGateView:
    """The gate plus the raw ledger projections (reporting / observability)."""

    gate: GateState
    equity: float
    peak_equity: float
    realized_today: float
    cumulative_realized: float
    open_notional: float
    open_position_count: int


def _to_position(row: PaperPositionRow) -> Position:
    return Position(
        symbol=row.symbol,
        timeframe=row.timeframe,
        units=float(row.units),
        entry_price=float(row.entry_price),
        entry_ts=row.entry_ts,
        side=Direction(row.side),
        stop_loss=None if row.stop_loss is None else float(row.stop_loss),
        take_profit=None if row.take_profit is None else float(row.take_profit),
        costs=float(row.costs),
    )


def _position_set(rows: list[PaperPositionRow]) -> PositionSet:
    return PositionSet([_to_position(row) for row in rows])


def _utc_day_start(now: datetime) -> datetime:
    aware = now.astimezone(UTC)
    return aware.replace(hour=0, minute=0, second=0, microsecond=0)


async def build_ledger_gate(*, broker: LedgerBroker, symbol: str, now: datetime) -> LedgerGateView:
    """Derive the live risk gate from the paper ledger (mirror of ``driver._gate``).

    Raises on any ledger-read failure so ``decide`` aborts and the transaction
    rolls back — promotion without evaluable risk state stays impossible.
    """
    sym = symbol.upper()
    open_rows = await broker.store.load_open_position_rows()
    positions = _position_set(open_rows)

    equity = broker.equity or broker.start_equity
    total_notional = positions.total_notional()
    exposure_pct = total_notional / equity if equity else 0.0
    correlation_pct = positions.basket_notional(sym) / equity if equity else 0.0
    correlation_triggered = positions.correlation_triggered(sym)

    day_start = _utc_day_start(now)
    day_end = day_start + timedelta(days=1)
    realized_today = await broker.realized_pnl_since(start_ts=day_start, end_ts=day_end)
    cumulative = await broker.realized_pnl_since(start_ts=_EPOCH, end_ts=now + timedelta(days=1))
    daily_loss_pct = max(0.0, -realized_today) / equity if equity else 0.0

    gate = GateState(
        exposure_used_pct=exposure_pct,
        correlation_used_pct=correlation_pct,
        daily_loss_used_pct=daily_loss_pct,
        drawdown_used_pct=broker.max_drawdown_pct,
        correlation_triggered=correlation_triggered,
    )

    for _name, _value in (
        ("exposure", exposure_pct),
        ("correlation", correlation_pct),
        ("daily_loss", daily_loss_pct),
        ("drawdown", broker.max_drawdown_pct),
    ):
        PAPER_GATE_USED_PCT.labels(gate=_name).observe(_value)

    logger.info(
        "gate_snapshot",
        symbol=sym,
        exposure_pct=round(exposure_pct, 6),
        correlation_pct=round(correlation_pct, 6),
        daily_loss_pct=round(daily_loss_pct, 6),
        drawdown_pct=round(broker.max_drawdown_pct, 6),
        equity=round(equity, 2),
        realized_loss=round(realized_today, 2),
        count_open=len(open_rows),
        include_pending=INCLUDE_PENDING_IN_EXPOSURE,
    )

    return LedgerGateView(
        gate=gate,
        equity=equity,
        peak_equity=broker.peak_equity,
        realized_today=realized_today,
        cumulative_realized=cumulative,
        open_notional=total_notional,
        open_position_count=len(open_rows),
    )
