"""Paper-trading lifecycle domain (Phase 13D).

The per-(symbol, timeframe, bar) processing sequence, kept DB-free so the
strict unit coverage gate sees the logic directly (no PostgreSQL required):

  1. ``restore_state()``            — ledger -> in-memory ``PaperBroker``
  2. fill / flip pending entries at the bar **OPEN** (deterministic
     arbitration; at most one fill per symbol per bar)
  3. evaluate SL/TP at the bar **CLOSE** (close-only, threshold exit)
  4. mark the position at the bar CLOSE
  5. account snapshot on state change (ts-deduped; observability only)

Everything here is pure orchestration over the paper-only ``LedgerBroker`` /
``LedgerStore`` seams; all fill / exit / PnL math is delegated to
``PaperBroker`` (the same deterministic engine the backtester uses), so results
stay byte-for-byte identical to a backtest for the same inputs.

Recovery identity (per unit): a PENDING order resumes at its deterministic fill
bar (``decision.bucket_ts + tf_seconds``); an OPEN position resumes at the
first closed bar of its timeframe whose bucket is strictly after ``entry_ts``.
``account_snapshots`` is never a correctness watermark — its ``ts`` is deduped
in application code under the single orchestrator writer.

SAFE MODE: this module is persistence / paper-simulation orchestration only.
It imports no session factory, no engine, no execution / routing / live surface,
and no broker connection. The orchestrator (single writer, under its token-
guarded Redis lock) is the only caller.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from app.agents.base import Direction
from app.broker.ledger import LedgerBroker, LedgerStore
from app.core.metrics import (
    PAPER_EXITS_TOTAL,
    PAPER_FILLS_TOTAL,
    PAPER_ORDERS_CANCELLED_TOTAL,
    PAPER_SNAPSHOTS_TOTAL,
)
from app.data.timeframes import Timeframe
from app.models.paper_ledger import PaperOrderRow, PaperPositionRow

#: Cancellation reasons visible on the ``paper_orders_cancelled_total`` metric.
CANCEL_SUPERSEDED: Final[str] = "superseded"
CANCEL_MISSING_BAR: Final[str] = "missing_bar"

_EPOCH: datetime = datetime(1970, 1, 1, tzinfo=UTC)

#: Fill disposition chosen by :func:`arbitrate_fills`.
FillAction = Literal["fill", "flip", "superseded"]


@dataclass(frozen=True, slots=True)
class PendingCandidate:
    """A PENDING order plus its deterministic fill bar (decision bucket + tf)."""

    order: PaperOrderRow
    fill_stamp: datetime


@dataclass(frozen=True, slots=True)
class Bar:
    """One processed unit-bar (a closed candle's open + close)."""

    ts: datetime
    open: float
    close: float


@dataclass(frozen=True, slots=True)
class UnitFrontier:
    """Ledger-derived recovery identity for one (symbol, timeframe) unit."""

    symbol: str
    timeframe: str
    pending: tuple[PendingCandidate, ...] = ()
    open_position: PaperPositionRow | None = None

    @property
    def fill_bars(self) -> frozenset[datetime]:
        """Deterministic fill bars of the unit's pending orders."""
        return frozenset(c.fill_stamp for c in self.pending)

    @property
    def resume_after(self) -> datetime | None:
        """For an OPEN position, resume at closed bars strictly after entry."""
        if self.open_position is None:
            return None
        return self.open_position.entry_ts

    @property
    def active(self) -> bool:
        return bool(self.pending) or self.open_position is not None


@dataclass(slots=True)
class UnitBarResult:
    """Outcome of one processed unit-bar (per-bar counters)."""

    symbol: str
    timeframe: str
    ts: datetime
    filled: int = 0
    exits: int = 0
    superseded: int = 0
    snapshot_written: bool = False


@dataclass(slots=True)
class UnitSummary:
    """Aggregate of a unit's processed bars (used by tests / reporting)."""

    symbol: str
    timeframe: str
    bars_processed: int = 0
    fills: int = 0
    exits: int = 0
    superseded: int = 0
    snapshots: int = 0
    depth_remaining: int = 0


def _created_at(order: PaperOrderRow) -> datetime:
    """Deterministic sort key for rows whose ``created_at`` is unset (tests)."""
    return order.created_at if order.created_at is not None else _EPOCH


def _key(c: PendingCandidate) -> tuple[datetime, datetime, uuid.UUID]:
    #: Earliest fill bar wins; ties break by created_at then id.
    return (c.fill_stamp, _created_at(c.order), c.order.id)


def fill_stamp(order: PaperOrderRow, decision_bucket: datetime) -> datetime:
    """The deterministic fill bar: the decision bucket plus the order's tf."""
    return decision_bucket + timedelta(seconds=Timeframe.seconds(order.timeframe))


def candidates_for_bar(
    candidates: Sequence[PendingCandidate], ts: datetime
) -> list[PendingCandidate]:
    """Candidates whose deterministic fill bar equals ``ts``, oldest first."""
    return sorted((c for c in candidates if c.fill_stamp == ts), key=_key)


def arbitrate_fills(
    open_side: Direction | None, candidates: Sequence[PendingCandidate]
) -> list[tuple[PendingCandidate, FillAction]]:
    """Deterministic fill arbitration for one (symbol, bar) at the OPEN.

    Iterates the (already ordered) candidates against the *current* in-memory
    position, mirroring the backtest driver's keep / close + re-open policy:

    - no open position          -> ``fill``
    - open position, same side  -> ``superseded`` (keep; driver keep-policy)
    - open position, opposing   -> ``flip`` (close at the open, then fill)

    At most one candidate acts per (symbol, bar): the first *actionable*
    candidate takes the bar, and every later candidate is superseded, so at most
    one position is ever created per (symbol, bar).
    """
    decisions: list[tuple[PendingCandidate, FillAction]] = []
    current = open_side
    filled = False
    for cand in candidates:
        side = Direction(cand.order.side)
        if current is None and not filled:
            decisions.append((cand, "fill"))
            current = side
            filled = True
        elif current == side:
            decisions.append((cand, "superseded"))
        elif current is not None and not filled:
            decisions.append((cand, "flip"))
            current = side
            filled = True
        else:
            decisions.append((cand, "superseded"))
    return decisions


def catchup_window(
    stamps: Sequence[datetime], max_bars: int
) -> tuple[list[datetime], int]:
    """Throttle window: ascending stamps, at most ``max_bars``; never a skip.

    Returns ``(to_process, depth_remaining)``. Anything beyond the throttle is
    processed in later cycles and tracked by ``paper_catchup_depth_remaining``.
    """
    ordered = sorted(set(stamps))
    to_process = ordered[: max(1, int(max_bars))]
    return to_process, max(0, len(ordered) - len(to_process))


def missing_bar_candidates(
    candidates: Sequence[PendingCandidate],
    candle_buckets: Sequence[datetime],
    *,
    latest_closed: datetime,
) -> list[PendingCandidate]:
    """PENDING orders whose fill bar has no closed candle by ``latest_closed``.

    A fill bar *after* the latest closed bar is simply not due yet (the bar has
    not closed); a price is never fabricated, so such orders are left pending.
    """
    present = set(candle_buckets)
    return sorted(
        (
            c
            for c in candidates
            if c.fill_stamp <= latest_closed and c.fill_stamp not in present
        ),
        key=_key,
    )


class PaperLifecycle:
    """DB-free domain driver of the per-unit-bar sequence (§9).

    Constructed per writer session with the same ``LedgerBroker`` / store pair
    the orchestrator uses; transactions are owned by the caller (one commit per
    unit-bar).
    """

    def __init__(
        self,
        *,
        store: LedgerStore,
        broker: LedgerBroker,
        decision_bucket: Callable[[uuid.UUID | None], Awaitable[datetime | None]]
        | None = None,
        snapshot_interval_seconds: int | None = None,
    ) -> None:
        self._store = store
        self._broker = broker
        self._decision_bucket: Callable[
            [uuid.UUID | None], Awaitable[datetime | None]
        ] = decision_bucket or store.get_decision_bucket
        self._snapshot_interval = snapshot_interval_seconds

    async def pending_candidates(self) -> list[PendingCandidate]:
        """All PENDING orders with a resolvable deterministic fill bar."""
        resolved: list[PendingCandidate] = []
        for order in await self._store.list_pending_orders():
            bucket = await self._decision_bucket(order.decision_id)
            if bucket is None:
                # No resolvable fill bar (orphaned / non-decision order): leave
                # pending rather than fabricating a bar.
                continue
            resolved.append(PendingCandidate(order=order, fill_stamp=fill_stamp(order, bucket)))
        return sorted(resolved, key=_key)

    async def frontier(
        self,
        *,
        symbol: str,
        timeframe: str,
        candidates: Sequence[PendingCandidate] | None = None,
        open_positions: Sequence[PaperPositionRow] | None = None,
    ) -> UnitFrontier:
        """Ledger-derived frontier for one (symbol, timeframe) unit (§14)."""
        sym = symbol.upper()
        if candidates is None:
            candidates = await self.pending_candidates()
        unit_pending = tuple(
            c for c in candidates if c.order.symbol == sym and c.order.timeframe == timeframe
        )
        open_p: PaperPositionRow | None = None
        if open_positions is not None:
            open_p = next(
                (
                    p
                    for p in open_positions
                    if p.symbol == sym and p.timeframe == timeframe
                ),
                None,
            )
        return UnitFrontier(
            symbol=sym, timeframe=timeframe, pending=unit_pending, open_position=open_p
        )

    async def reconcile(self) -> bool:
        """Ledger view == restored broker view (fail closed on any mismatch).

        Compares store open positions + pending orders against the restored
        in-memory broker's view. A mismatch is a bug (single writer), not drift:
        the cycle aborts and emits ``alert.paper_reconcile``.
        """
        store_open = {p.symbol for p in await self._store.list_open_positions()}
        broker_open = self._broker.open_symbols
        if store_open != broker_open:
            return False
        store_pending = {o.id for o in await self._store.list_pending_orders()}
        broker_pending = {o.id for o in self._broker.pending}
        return store_pending == broker_pending

    async def process_unit_bar(
        self,
        *,
        symbol: str,
        timeframe: str,
        bar: Bar,
        candidates: Sequence[PendingCandidate],
    ) -> UnitBarResult:
        """Run the §9 sequence for ONE unit-bar.

        restore -> fills/flips at the OPEN -> SL/TP at the CLOSE -> mark ->
        snapshot (on state change, ts-deduped). Persists via flush only; the
        caller commits (one unit-bar transaction).
        """
        await self._broker.restore_state()
        sym = symbol.upper()
        result = UnitBarResult(symbol=sym, timeframe=timeframe, ts=bar.ts)

        open_pos = self._broker.open_for(sym)
        open_side = None if open_pos is None else open_pos.side
        for cand, action in arbitrate_fills(
            open_side, candidates_for_bar(candidates, bar.ts)
        ):
            if action == "superseded":
                if (
                    await self._broker.cancel_pending(cand.order, reason=CANCEL_SUPERSEDED)
                    is not None
                ):
                    result.superseded += 1
                    PAPER_ORDERS_CANCELLED_TOTAL.labels(reason=CANCEL_SUPERSEDED).inc()
                continue
            if action == "flip":
                await self._broker.close_on_signal(symbol=sym, price=bar.open, ts=bar.ts)
            if (
                await self._broker.fill_pending(cand.order, open_price=bar.open, ts=bar.ts)
                is not None
            ):
                result.filled += 1
                PAPER_FILLS_TOTAL.inc()

        trade = await self._broker.evaluate_exit(symbol=sym, close=bar.close, ts=bar.ts)
        if trade is not None:
            result.exits += 1
            PAPER_EXITS_TOTAL.labels(reason=trade.exit_reason).inc()

        await self._broker.mark_position(symbol=sym, price=bar.close)

        state_changed = (
            result.filled > 0 or result.exits > 0 or self._broker.open_for(sym) is not None
        )
        if state_changed and await self._snapshot_due(bar):
            await self._broker.equity_snapshot(ts=bar.ts)
            result.snapshot_written = True
            PAPER_SNAPSHOTS_TOTAL.inc()
        return result

    async def process_unit(
        self,
        *,
        symbol: str,
        timeframe: str,
        bars: Sequence[Bar],
        candidates: Sequence[PendingCandidate] | None = None,
        max_bars: int | None = None,
    ) -> UnitSummary:
        """Convenience loop over a unit's bars (throttled, ascending).

        The orchestrator instead drives :meth:`process_unit_bar` per bar so each
        bar commits independently; this aggregate form keeps the same sequence.
        """
        summary = UnitSummary(symbol=symbol.upper(), timeframe=timeframe)
        if not bars:
            return summary
        if candidates is None:
            candidates = await self.pending_candidates()
        unit_candidates = [
            c
            for c in candidates
            if c.order.symbol == symbol.upper() and c.order.timeframe == timeframe
        ]
        if max_bars is None:
            max_bars = len(bars)
        window, depth = catchup_window([b.ts for b in bars], max_bars)
        summary.depth_remaining = depth
        window_ts = set(window)
        to_run = {b.ts: b for b in bars if b.ts in window_ts}
        for bar in sorted(to_run.values(), key=lambda b: b.ts):
            r = await self.process_unit_bar(
                symbol=symbol, timeframe=timeframe, bar=bar, candidates=unit_candidates
            )
            summary.bars_processed += 1
            summary.fills += r.filled
            summary.exits += r.exits
            summary.superseded += r.superseded
            summary.snapshots += 1 if r.snapshot_written else 0
        return summary

    async def _snapshot_due(self, bar: Bar) -> bool:
        """Write an account snapshot only when none exists at/after this bar."""
        latest = await self._store.latest_snapshot()
        if latest is None:
            return True
        within_interval = (
            self._snapshot_interval is not None
            and bar.ts < latest.ts + timedelta(seconds=self._snapshot_interval)
        )
        return not (latest.ts >= bar.ts or within_interval)
