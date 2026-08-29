"""Pure paper position & PnL math (Phase 6).

Positions are the broker's exposure source of truth during a backtest. This
module is synchronous and free of I/O so it is fully unit-tested by the strict
coverage gate.

SAFE MODE: this is paper/analysis bookkeeping only. There is no order routing,
no broker connection, and no live execution path anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.agents.base import Direction


@dataclass(frozen=True, slots=True)
class Position:
    """One open simulated paper position."""

    symbol: str
    timeframe: str
    units: float
    entry_price: float
    entry_ts: datetime
    side: Direction  # LONG or SHORT only (never FLAT)
    stop_loss: float | None = None
    take_profit: float | None = None
    costs: float = 0.0

    @property
    def notional(self) -> float:
        return self.units * self.entry_price

    def unrealized(self, price: float) -> float:
        """Unrealized PnL (in quote units) at the given mid/last price."""
        delta = (
            price - self.entry_price if self.side == Direction.LONG else self.entry_price - price
        )
        return self.units * delta

    def exit_price_for(
        self, price: float, *, sl: float | None, tp: float | None
    ) -> tuple[float, str, bool]:
        """Resolve the exit price for a next-bar close given SL/TP thresholds.

        Returns ``(exit_price, reason, hit)``. Conservative policy: an
        ambiguous bar (both SL and TP touched within the same next bar) is not
        assumed to resolve favorably — we only act on the bar's *close*,
        checking whether it breaches stop/take-profit. A breach exits at the
        threshold price (never better than the threshold).
        """
        if self.side == Direction.LONG:
            if sl is not None and price <= sl:
                return sl, "stop_loss", True
            if tp is not None and price >= tp:
                return tp, "take_profit", True
        else:  # SHORT
            if sl is not None and price >= sl:
                return sl, "stop_loss", True
            if tp is not None and price <= tp:
                return tp, "take_profit", True
        return price, "signal", False


@dataclass(slots=True)
class PositionSet:
    """Ordered collection of open positions keyed deterministically."""

    positions: list[Position] = field(default_factory=list)

    def open_for(self, symbol: str) -> Position | None:
        for p in self.positions:
            if p.symbol == symbol:
                return p
        return None

    def total_notional(self) -> float:
        return sum(p.notional for p in self.positions)

    def basket_notional(self, symbol: str) -> float:
        target = {symbol[:3], symbol[3:]}
        return sum(
            p.notional for p in self.positions if p.side == Direction.LONG and _touches(p, target)
        )

    def correlation_triggered(self, symbol: str) -> bool:
        target = {symbol[:3], symbol[3:]}
        return any(p.side == Direction.LONG and _touches(p, target) for p in self.positions)

    def close(self, symbol: str) -> bool:
        for idx, p in enumerate(self.positions):
            if p.symbol == symbol:
                del self.positions[idx]
                return True
        return False


def _touches(p: Position, target: set[str]) -> bool:
    return bool({p.symbol[:3], p.symbol[3:]} & target)
