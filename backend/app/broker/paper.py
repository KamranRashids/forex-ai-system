"""Deterministic paper broker for backtests (Phase 6).

The PaperBroker simulates fills on a cash/equity basis and is the exposed
position source of truth during a backtest (the decision risk gate reads its
open positions rather than any DB ledger). It is intentionally PAPER ONLY:
it performs simulated fills and bookkeeping against a starting equity; it never
creates orders, never touches ``orders_paper``/``positions``, and has no
broker/live-execution path.

Execution policy (decisions D-C / no look-ahead):
- Orders are generated at the close of a bar and filled at the **open of the
  next bar**.
- SL/TP are evaluated on *subsequent bar closes only* (conservative; no
  favorable intrabar ordering assumed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.agents.base import Direction
from app.broker.costs import CostParams, cost_per_unit
from app.broker.positions import Position, PositionSet

CASH_START: float = 100_000.0

EXIT_SIGNAL = "signal"
EXIT_SL = "stop_loss"
EXIT_TP = "take_profit"
EXIT_FLAT = "flat"


@dataclass(frozen=True, slots=True)
class Trade:
    """A closed round-trip for reporting."""

    symbol: str
    timeframe: str
    side: Direction
    units: float
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    gross_pnl: float
    costs: float
    net_pnl: float
    exit_reason: str


@dataclass(slots=True)
class BrokerState:
    """Snapshot of the broker after a bar is processed."""

    ts: datetime
    equity: float
    cash: float
    open_positions: PositionSet
    trades: list[Trade] = field(default_factory=list)


class PaperBroker:
    """Deterministic cash-based simulated broker."""

    def __init__(
        self,
        *,
        start_equity: float = CASH_START,
        seed: int = 0,
        cost_params: CostParams | None = None,
    ) -> None:
        self.start_equity = start_equity
        self.cash = start_equity
        self.seed = seed
        self.cost_params = cost_params or CostParams()
        self.equity = start_equity
        self.peak_equity = start_equity
        self.max_drawdown_pct = 0.0
        self.positions = PositionSet()
        self.trades: list[Trade] = []
        #: per-symbol last mark price (driver updates after each closed bar).
        self.last_mark: dict[str, float] = {}

    # --- fills -----------------------------------------------------------------

    def mark_position(self, symbol: str, price: float) -> None:
        """Record the current mark price for ``symbol`` (used for equity)."""
        self.last_mark[symbol] = price

    def total_equity(self) -> float:
        unrealized = 0.0
        for p in self.positions.positions:
            mark = self.last_mark.get(p.symbol, p.entry_price)
            unrealized += p.unrealized(mark)
        return self.cash + unrealized

    def enter_at_next_open(
        self,
        *,
        symbol: str,
        timeframe: str,
        direction: Direction,
        ref_price: float,
        ts: datetime,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        units: float | None = None,
    ) -> None:
        """Open a position at the *current* bar's open (the "next" bar after the
        decision bar). ``ref_price`` is the open of this fill bar. Entry costs
        are deducted immediately.
        """
        if direction == Direction.FLAT or units is None or units <= 0:
            return
        if self.positions.open_for(symbol) is not None:
            return
        price = ref_price
        cost = cost_per_unit(
            price=price,
            seed=self.seed,
            symbol=symbol,
            side=direction.value,
            notional=units * price,
            params=self.cost_params,
        )
        cost_total = cost * units
        pos = Position(
            symbol=symbol,
            timeframe=timeframe,
            units=units,
            entry_price=price,
            entry_ts=ts,
            side=direction,
            stop_loss=stop_loss,
            take_profit=take_profit,
            costs=cost_total,
        )
        self.cash -= cost_total
        self.last_mark[symbol] = price
        self.positions.positions.append(pos)

    def evaluate_exit(self, *, symbol: str, close: float, ts: datetime) -> Trade | None:
        """Evaluate SL/TP against a closed bar's close; conservative.

        A breach exits at the threshold price. No favorable intrabar ordering
        is assumed. Returns the closed Trade, or None if nothing closed here.
        """
        pos = self.positions.open_for(symbol)
        if pos is None:
            return None
        exit_price, reason, hit = pos.exit_price_for(close, sl=pos.stop_loss, tp=pos.take_profit)
        if not hit:
            return None
        return self._close(symbol=symbol, price=exit_price, reason=reason, ts=ts)

    def close_on_signal(self, *, symbol: str, price: float, ts: datetime) -> Trade | None:
        """Close an open position on a flat/opposing next-bar signal."""
        pos = self.positions.open_for(symbol)
        if pos is None:
            return None
        return self._close(symbol=symbol, price=price, reason=EXIT_SIGNAL, ts=ts)

    def _close(self, *, symbol: str, price: float, reason: str, ts: datetime) -> Trade:
        pos = self.positions.open_for(symbol)
        assert pos is not None  # guarded by callers
        cost = cost_per_unit(
            price=price,
            seed=self.seed,
            symbol=symbol,
            side=pos.side.value,
            notional=pos.units * price,
            params=self.cost_params,
        )
        cost_total = cost * pos.units + (pos.costs or 0.0)
        gross = (
            pos.units * (price - pos.entry_price)
            if pos.side == Direction.LONG
            else pos.units * (pos.entry_price - price)
        )
        net = gross - cost_total
        self.cash += gross
        self.cash -= cost_total
        trade = Trade(
            symbol=symbol,
            timeframe=pos.timeframe,
            side=pos.side,
            units=pos.units,
            entry_ts=pos.entry_ts,
            entry_price=pos.entry_price,
            exit_ts=ts,
            exit_price=price,
            gross_pnl=round(gross, 6),
            costs=round(cost_total, 6),
            net_pnl=round(net, 6),
            exit_reason=reason,
        )
        self.trades.append(trade)
        self.positions.close(symbol)
        return trade

    def state(self, ts: datetime) -> BrokerState:
        equity = self.total_equity()
        self.equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity
        if self.peak_equity > 0:
            dd = (self.peak_equity - equity) / self.peak_equity
            if dd > self.max_drawdown_pct:
                self.max_drawdown_pct = dd
        return BrokerState(
            ts=ts,
            equity=equity,
            cash=self.cash,
            open_positions=self.positions,
            trades=list(self.trades),
        )
