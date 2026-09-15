"""BrokerAdapter: the common surface every broker satisfies (Phase 13, Phase A).

The only concrete implementation today is the paper ``LedgerBroker``
(app/broker/ledger.py); the interface exists so a future paper executor can
drive the ledger through one stable, minimal contract without ever referencing
a live venue.

SAFE MODE: this protocol is deliberately paper-only. It describes simulated
fills, positions, and account equity — there is no order-routing or live
execution method by construction, and no amount of wiring can make its
operations send a real order.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol

from app.agents.base import Direction
from app.broker.paper import Trade
from app.models.paper_ledger import (
    AccountSnapshotRow,
    PaperOrderRow,
    PaperPositionRow,
)


class BrokerAdapter(Protocol):
    """Minimal, paper-only contract for opening/closing/valuing positions.

    Entry is deferred: ``submit_paper_order`` persists a PENDING order (Phase
    13D) and a later lifecycle step calls ``fill_pending`` at the order's
    deterministic next-bar open, producing the position row.
    """

    async def submit_paper_order(
        self,
        *,
        symbol: str,
        timeframe: str,
        direction: Direction,
        ref_price: float,
        ts: datetime,
        units: float,
        decision_id: uuid.UUID | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> PaperOrderRow | None:
        """Persist a PENDING paper order linked to a decision.

        Returns the pending order on success, or None when the order is
        rejected (FLAT / non-positive units, or an open position on the same
        side already exists — mirror of the backtest's keep-policy).
        """
        ...

    async def fill_pending(
        self, order: PaperOrderRow, *, open_price: float, ts: datetime
    ) -> PaperPositionRow | None:
        """Fill a PENDING order at the next bar's open; persists FILLED + OPEN."""
        ...

    async def cancel_pending(self, order: PaperOrderRow, *, reason: str) -> PaperOrderRow | None:
        """Cancel a PENDING order (superseded / missing fill bar)."""
        ...

    async def evaluate_exit(self, *, symbol: str, close: float, ts: datetime) -> Trade | None:
        """Evaluate SL/TP against a closed bar's close; returns the closed Trade if any."""
        ...

    async def close_on_signal(self, *, symbol: str, price: float, ts: datetime) -> Trade | None:
        """Close an open position on a flat/opposing next-bar signal."""
        ...

    async def mark_position(self, *, symbol: str, price: float) -> None:
        """Record the current mark price for ``symbol`` (feeds equity, not persisted)."""
        ...

    async def equity_snapshot(self, *, ts: datetime) -> AccountSnapshotRow:
        """Persist and return the current account equity snapshot."""
        ...

    async def restore_state(self) -> None:
        """Rebuild the in-memory broker state from the persisted ledger."""
        ...
