"""Asynchronous, DB-backed PAPER broker (Phase 13, Phase A).

``LedgerBroker`` is the only ``BrokerAdapter`` implementation (paper-only). All
simulation math — entry fill, exit fill, costs, equity, drawdown — is delegated
to the existing synchronous ``PaperBroker`` (the same deterministic engine the
backtester uses); ``LedgerBroker`` only *persists* the resulting states through
a :class:`LedgerStore` (orders_paper / positions / account_snapshots).

Why not duplicate the math: a paper order filled by the ledger must agree
exactly with a backtest fill for the same inputs. Reusing ``PaperBroker``
guarantees that parity with zero drift, and the strict unit coverage gate sees
the same logic it already covers.

Entry policy (Phase 13D): entry is deferred, not immediate.

- ``submit_paper_order`` persists a ``PENDING`` order linked to the decision
  (requested price = the risk evaluation's reference price; no position, no
  ``PaperBroker`` mutation). A same-side open position is a deterministic
  no-op (keep-policy, matching the backtest driver), while an opposing open
  position still submits — the lifecycle flips at the next-bar open.
- ``fill_pending`` is the lifecycle's fill seam: it fills the order at the
  deterministic next-bar open of the order's timeframe (``filled_price`` = the
  open, ``filled_at`` = the fill-bar bucket) and persists a FILLED order + OPEN
  position, delegating all math to ``PaperBroker.enter_at_next_open``.

SAFE MODE: persistence only. There is no order routing, no broker connection,
and no live-execution path. Nothing in this module auto-subscribes PAPER
decisions; the orchestrator's paper lifecycle drives this class explicitly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from app.agents.base import Direction
from app.broker.costs import CostParams
from app.broker.paper import EXIT_SIGNAL, BrokerState, PaperBroker, Trade
from app.broker.positions import Position
from app.models.paper_ledger import (
    AccountSnapshotRow,
    PaperOrderRow,
    PaperOrderStatus,
    PaperOrderType,
    PaperPositionRow,
    PaperPositionStatus,
)


def _d(value: int | float | Decimal) -> Decimal:
    """Exact Decimal conversion (mirrors backtest repository, avoids binary float error)."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class LedgerStore(Protocol):
    """Persistence seam the broker writes through (in-memory in unit tests)."""

    async def save_order(self, order: PaperOrderRow) -> None: ...
    async def update_order(self, order: PaperOrderRow) -> None: ...
    async def list_pending_orders(self) -> list[PaperOrderRow]: ...
    async def get_decision_bucket(self, decision_id: uuid.UUID | None) -> datetime | None: ...
    async def save_position(self, position: PaperPositionRow) -> None: ...
    async def get_open_position(self, symbol: str) -> PaperPositionRow | None: ...
    async def list_open_positions(self) -> list[PaperPositionRow]: ...
    async def list_closed_positions(self) -> list[PaperPositionRow]: ...
    async def update_position(self, position: PaperPositionRow) -> None: ...
    async def save_snapshot(self, snapshot: AccountSnapshotRow) -> None: ...
    async def latest_snapshot(self) -> AccountSnapshotRow | None: ...


class LedgerBroker:
    """Async paper broker whose fills are persisted to the paper ledger."""

    def __init__(
        self,
        *,
        store: LedgerStore,
        start_equity: float = 100_000.0,
        seed: int = 0,
        cost_params: CostParams | None = None,
    ) -> None:
        self._store = store
        self._seed = seed
        #: The single source of truth for simulation math (fill/cost/equity).
        self._paper = PaperBroker(start_equity=start_equity, seed=seed, cost_params=cost_params)
        #: Pending orders restored from the ledger (rebuilds after ``restore_state``).
        self._pending: list[PaperOrderRow] = []

    # --- projection -----------------------------------------------------------

    @property
    def store(self) -> LedgerStore:
        """The persistence seam this broker writes through (same-session store)."""
        return self._store

    def state(self, ts: datetime) -> BrokerState:
        """Live projected broker state (not yet persisted)."""
        return self._paper.state(ts)

    def open_for(self, symbol: str) -> Position | None:
        """In-memory open position for ``symbol`` (post-restore view)."""
        return self._paper.positions.open_for(symbol.upper())

    @property
    def open_symbols(self) -> set[str]:
        """Symbols holding an in-memory open position (post-restore view)."""
        return {p.symbol for p in self._paper.positions.positions}

    @property
    def pending(self) -> list[PaperOrderRow]:
        """Pending orders as restored from the ledger (oldest first)."""
        return list(self._pending)

    # --- entry (deferred: submit -> lifecycle fill) -----------------------------

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
        """Persist a PENDING paper order linked to a decision (no fill yet).

        Guards (fail closed):
        - FLAT / non-positive units -> None;
        - an open position with the *same side* -> None (keep-policy, mirror of
          the backtest driver scheduling fills: `driver.py` lines 248-249);
        - an opposing open position still submits — the lifecycle closes and
          re-opens at the next-bar open.

        ``ref_price`` is the risk evaluation's reference price (requested, not
        guaranteed); ``ts`` records the decision-bar context for audit. The row
        keeps ``seed`` so the lifecycle fills with the same deterministic costs.
        """
        sym = symbol.upper()
        if direction == Direction.FLAT or units <= 0:
            return None
        pos = self._paper.positions.open_for(sym)
        if pos is not None and pos.side == direction:
            return None
        # One order per decision, even while PENDING (restored + this session):
        # a redelivered decision must never create a second pending row. The
        # DB's uq_orders_paper_decision_id is the backstop.
        if decision_id is not None and any(o.decision_id == decision_id for o in self._pending):
            return None
        order = PaperOrderRow(
            id=uuid.uuid4(),
            decision_id=decision_id,
            symbol=sym,
            timeframe=timeframe,
            side=direction.value,
            order_type=PaperOrderType.NEXT_OPEN.value,
            status=PaperOrderStatus.PENDING.value,
            units=_d(units),
            requested_price=_d(ref_price),
            filled_price=None,
            costs=Decimal("0"),
            stop_loss=None if stop_loss is None else _d(stop_loss),
            take_profit=None if take_profit is None else _d(take_profit),
            seed=self._seed,
            filled_at=None,
        )
        self._pending.append(order)
        await self._store.save_order(order)
        return order

    async def fill_pending(
        self, order: PaperOrderRow, *, open_price: float, ts: datetime
    ) -> PaperPositionRow | None:
        """Fill a PENDING order at the next bar's open; persist FILLED + OPEN.

        Delegates the fill (entry price, deterministic entry costs, cash
        deduction) to ``PaperBroker.enter_at_next_open`` so results are
        byte-for-byte identical to a backtest fill for the same inputs.
        ``filled_price`` = the open, ``filled_at`` = ``ts`` (the fill-bar
        bucket). Returns None when the order is not PENDING or the symbol
        already has an open position.
        """
        sym = order.symbol.upper()
        if order.status != PaperOrderStatus.PENDING.value:
            return None
        if self._paper.positions.open_for(sym) is not None:
            return None
        # Fill with the deterministic seed recorded at submit time.
        self._paper.seed = order.seed
        self._paper.enter_at_next_open(
            symbol=sym,
            timeframe=order.timeframe,
            direction=Direction(order.side),
            ref_price=float(open_price),
            ts=ts,
            stop_loss=None if order.stop_loss is None else float(order.stop_loss),
            take_profit=None if order.take_profit is None else float(order.take_profit),
            units=float(order.units),
        )
        pos = self._paper.positions.open_for(sym)
        if pos is None:
            return None
        order.status = PaperOrderStatus.FILLED.value
        order.filled_price = _d(pos.entry_price)
        order.costs = _d(pos.costs)
        order.filled_at = ts
        position = PaperPositionRow(
            id=uuid.uuid4(),
            order_id=order.id,
            symbol=pos.symbol,
            timeframe=pos.timeframe,
            side=pos.side.value,
            units=_d(pos.units),
            entry_price=_d(pos.entry_price),
            entry_ts=pos.entry_ts,
            stop_loss=None if pos.stop_loss is None else _d(pos.stop_loss),
            take_profit=None if pos.take_profit is None else _d(pos.take_profit),
            costs=_d(pos.costs),
            status=PaperPositionStatus.OPEN.value,
        )
        self._pending = [o for o in self._pending if o.id != order.id]
        await self._store.update_order(order)
        await self._store.save_position(position)
        return position

    async def cancel_pending(self, order: PaperOrderRow, *, reason: str) -> PaperOrderRow | None:
        """Cancel a PENDING order (superseded / missing fill bar)."""
        if order.status != PaperOrderStatus.PENDING.value:
            return None
        order.status = PaperOrderStatus.CANCELLED.value
        self._pending = [o for o in self._pending if o.id != order.id]
        await self._store.update_order(order)
        return order

    async def evaluate_exit(self, *, symbol: str, close: float, ts: datetime) -> Trade | None:
        """Evaluate SL/TP against a closed bar's close; persist any close."""
        trade = self._paper.evaluate_exit(symbol=symbol, close=close, ts=ts)
        if trade is not None:
            await self._persist_close(symbol=trade.symbol, trade=trade)
        return trade

    async def close_on_signal(self, *, symbol: str, price: float, ts: datetime) -> Trade | None:
        """Close an open position on a flat/opposing next-bar signal."""
        trade = self._paper.close_on_signal(symbol=symbol, price=price, ts=ts)
        if trade is not None:
            await self._persist_close(symbol=trade.symbol, trade=trade)
        return trade

    async def mark_position(self, *, symbol: str, price: float) -> None:
        """Record a mark price (feeds equity valuation; not persisted directly)."""
        self._paper.mark_position(symbol, price)

    # --- accounting -----------------------------------------------------------

    async def equity_snapshot(self, *, ts: datetime) -> AccountSnapshotRow:
        """Persist a point-in-time equity snapshot (cash, equity, PnL, drawdown)."""
        state = self._paper.state(ts)
        realized = sum((_d(t.net_pnl) for t in self._paper.trades), start=Decimal("0"))
        snapshot = AccountSnapshotRow(
            id=uuid.uuid4(),
            ts=ts,
            cash=_d(state.cash),
            equity=_d(state.equity),
            open_pnl=_d(state.equity - state.cash),
            realized_pnl=realized,
            peak_equity=_d(self._paper.peak_equity),
            drawdown_pct=_d(self._paper.max_drawdown_pct),
            margin_used=Decimal("0"),
            extra={},
        )
        await self._store.save_snapshot(snapshot)
        return snapshot

    # --- lifecycle ------------------------------------------------------------

    async def restore_state(self) -> None:
        """Rebuild the in-memory broker from the persisted ledger.

        Cash/equity/drawdown come from the latest snapshot; open positions are
        re-materialized (marked at their entry price so ``open_pnl`` starts at
        zero), and historic realized PnL is rebuilt from closed positions so
        subsequent snapshots report cumulative numbers.
        """
        latest = await self._store.latest_snapshot()
        if latest is not None:
            self._paper.cash = float(latest.cash)
            self._paper.equity = float(latest.equity)
            self._paper.peak_equity = float(latest.peak_equity)
            self._paper.max_drawdown_pct = float(latest.drawdown_pct)

        open_rows = await self._store.list_open_positions()
        self._paper.positions.positions.clear()
        self._paper.last_mark.clear()
        for row in open_rows:
            self._paper.positions.positions.append(
                Position(
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
            )
            self._paper.last_mark[row.symbol] = float(row.entry_price)

        closed_rows = await self._store.list_closed_positions()
        self._paper.trades = [
            Trade(
                symbol=row.symbol,
                timeframe=row.timeframe,
                side=Direction(row.side),
                units=float(row.units),
                entry_ts=row.entry_ts,
                entry_price=float(row.entry_price),
                exit_ts=row.exit_ts if row.exit_ts is not None else row.updated_at,
                exit_price=float(row.exit_price if row.exit_price is not None else row.entry_price),
                gross_pnl=float(row.gross_pnl if row.gross_pnl is not None else Decimal("0")),
                costs=float(row.costs),
                net_pnl=float(row.net_pnl if row.net_pnl is not None else Decimal("0")),
                exit_reason=row.exit_reason if row.exit_reason is not None else EXIT_SIGNAL,
            )
            for row in closed_rows
        ]

        #: Rebuild the pending queue so delayed fills resume after a restart.
        pending = await self._store.list_pending_orders()
        self._pending = sorted(
            pending,
            key=lambda o: (o.created_at or datetime.min.replace(tzinfo=UTC), o.id),
        )

    # --- internals ------------------------------------------------------------

    async def _persist_close(self, *, symbol: str, trade: Trade) -> None:
        row = await self._store.get_open_position(symbol.upper())
        if row is None:
            return
        row.status = PaperPositionStatus.CLOSED.value
        row.exit_price = _d(trade.exit_price)
        row.exit_ts = trade.exit_ts
        row.exit_reason = trade.exit_reason
        row.gross_pnl = _d(trade.gross_pnl)
        row.net_pnl = _d(trade.net_pnl)
        row.costs = _d(trade.costs)
        await self._store.update_position(row)
