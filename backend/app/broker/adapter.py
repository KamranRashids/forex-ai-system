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
from app.models.paper_ledger import AccountSnapshotRow, PaperOrderRow


class BrokerAdapter(Protocol):
    """Minimal, paper-only contract for opening/closing/valuing positions."""

    async def open_at_next_open(
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
        """Request a fill at the next bar's open.

        Returns the persisted order on success, or None when the order is
        rejected (e.g. a position is already open for the symbol, or the
        direction is FLAT / units are non-positive).
        """
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
