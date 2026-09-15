"""Unit tests: Phase 13C PAPER decision -> LedgerBroker wiring (pure seam).

DB-free by construction: these exercise ``_wire_paper_decision``, the pure
bridge between a freshly persisted PAPER decision and the paper-only broker
gateway, over an in-memory ``LedgerStore``. Guards under test:

- only a PAPER decision that *this writer just created* may reach the broker
  (ANALYSIS / BLOCKED / replays are no-ops);
- the persisted risk evaluation is the single sizing authority — wiring never
  re-sizes and cannot manufacture a position without a risk-approved sizing;
- the broker persists a PENDING order linked to the decision (no position yet —
  the Phase 13D lifecycle fills it at the next bar's open);
- broker rejection (FLAT / same-side position open / duplicate decision id)
  returns ``None`` without raising; broker failure propagates so the caller
  rolls back the whole transaction (decision + order + position consistency);
- the only broker surface is paper-only (no submit/route/place/live member).

(Phase 13C / Phase 13D)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.agents.base import Direction
from app.broker.ledger import LedgerBroker
from app.decisions.engine import DecideResult, DecisionAction
from app.models.decision import DecisionRow, DecisionStatus
from app.models.paper_ledger import (
    AccountSnapshotRow,
    PaperOrderRow,
    PaperOrderStatus,
    PaperPositionRow,
    PaperPositionStatus,
)
from app.models.risk_evaluation import RiskEvaluationRow
from app.workers.orchestrator_worker import PaperBrokerGateway, _wire_paper_decision

_BUCKET = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_NOW = _BUCKET + timedelta(hours=1)
_VALID_UNTIL = _BUCKET + timedelta(hours=12)


class InMemoryStore:
    """Pure-Python LedgerStore used by the DB-free unit tests."""

    def __init__(self) -> None:
        self.orders: list[PaperOrderRow] = []
        self.positions: list[PaperPositionRow] = []
        self.snapshots: list[AccountSnapshotRow] = []

    async def save_order(self, order: PaperOrderRow) -> None:
        self.orders.append(order)

    async def update_order(self, order: PaperOrderRow) -> None:
        for i, existing in enumerate(self.orders):
            if existing.id == order.id:
                self.orders[i] = order
                return

    async def list_pending_orders(self) -> list[PaperOrderRow]:
        return [
            order
            for order in self.orders
            if order.status == PaperOrderStatus.PENDING.value
        ]

    async def get_decision_bucket(
        self, decision_id: uuid.UUID | None
    ) -> datetime | None:
        return _BUCKET if decision_id is not None else None

    async def save_position(self, position: PaperPositionRow) -> None:
        self.positions.append(position)

    async def get_open_position(self, symbol: str) -> PaperPositionRow | None:
        return next(
            (
                p
                for p in self.positions
                if p.symbol == symbol.upper() and p.status == PaperPositionStatus.OPEN.value
            ),
            None,
        )

    async def list_open_positions(self) -> list[PaperPositionRow]:
        return [p for p in self.positions if p.status == PaperPositionStatus.OPEN.value]

    async def list_closed_positions(self) -> list[PaperPositionRow]:
        return [p for p in self.positions if p.status == PaperPositionStatus.CLOSED.value]

    async def update_position(self, position: PaperPositionRow) -> None:
        return None

    async def save_snapshot(self, snapshot: AccountSnapshotRow) -> None:
        self.snapshots.append(snapshot)

    async def latest_snapshot(self) -> AccountSnapshotRow | None:
        return self.snapshots[-1] if self.snapshots else None


def _result(
    *,
    status: DecisionStatus = DecisionStatus.PAPER,
    created: bool = True,
    direction: Direction = Direction.LONG,
) -> DecideResult:
    return DecideResult(
        symbol="EURUSD",
        timeframe="H1",
        bucket_ts=_BUCKET,
        action=DecisionAction.PERSIST,
        created=created,
        status=status,
        direction=direction,
        confidence=0.7,
        agreement=0.7,
        coverage=1.0,
        veto_code=None,
        skip_reason=None,
        inputs_hash="0" * 64,
    )


def _decision() -> DecisionRow:
    return DecisionRow(
        id=uuid.uuid4(),
        run_id="",
        symbol="EURUSD",
        timeframe="H1",
        bucket_ts=_BUCKET,
        fused_direction="LONG",
        confidence=Decimal("0.7000"),
        agreement=Decimal("0.7000"),
        status=DecisionStatus.PAPER.value,
        veto_code=None,
        veto_reason=None,
        inputs_hash="0" * 64,
        weights={},
        code_versions={},
        rationale=None,
        decision_at=_NOW,
        valid_until=_VALID_UNTIL,
    )


def _risk_eval(
    *,
    units: str = "10000.000000",
    price: str = "1.10000000",
    stop_loss: str = "1.09000000",
    take_profit: str = "1.12000000",
) -> RiskEvaluationRow:
    return RiskEvaluationRow(
        id=uuid.uuid4(),
        decision_id=None,
        symbol="EURUSD",
        timeframe="H1",
        bucket_ts=_BUCKET,
        position_size_units=Decimal(units),
        price=Decimal(price),
        atr=Decimal("0.00500000"),
        stop_loss=Decimal(stop_loss),
        take_profit=Decimal(take_profit),
        rr_ratio=Decimal("1.5000"),
        risk_pct_account=Decimal("0.0100"),
        exposure_ok=True,
        correlation_ok=True,
        daily_loss_ok=True,
        drawdown_ok=True,
        passed=True,
        reasons=[],
        evaluated_at=_NOW,
    )


class _RaisingBroker:
    """A broker whose submit seam always fails (simulates a DB/broker crash)."""

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
        raise RuntimeError("simulated broker failure")


# ---------------------------------------------------------------------------
# PAPER creates a PENDING order linked to the decision (no position yet).
# ---------------------------------------------------------------------------


async def test_paper_decision_creates_order_and_position():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    decision = _decision()

    order = await _wire_paper_decision(
        broker, result=_result(), decision=decision, risk_eval=_risk_eval()
    )

    assert order is not None
    assert order.status == PaperOrderStatus.PENDING.value
    assert order.decision_id == decision.id
    assert order.symbol == "EURUSD"
    assert order.side == "LONG"
    assert order.filled_price is None  # not filled until the next bar's open
    assert float(order.units) == pytest.approx(10_000.0, abs=1e-9)
    assert float(order.stop_loss) == pytest.approx(1.09, abs=1e-9)
    assert float(order.take_profit) == pytest.approx(1.12, abs=1e-9)
    assert float(order.costs) == 0.0  # entry cost is only deducted at the fill

    pos = await store.get_open_position("EURUSD")
    assert pos is None  # PENDING only — the lifecycle fills at the next open
    assert not store.positions


async def test_decision_id_linkage_persists_on_order():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=1)
    decision = _decision()

    order = await _wire_paper_decision(
        broker, result=_result(), decision=decision, risk_eval=_risk_eval()
    )

    assert order is not None
    assert order.decision_id == decision.id
    # The broker persisted the decision id through to the order row.
    assert store.orders[0].decision_id == decision.id


# ---------------------------------------------------------------------------
# ANALYSIS / BLOCKED never reach the broker.
# ---------------------------------------------------------------------------


async def test_analysis_decision_does_not_create_order():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)

    order = await _wire_paper_decision(
        broker,
        result=_result(status=DecisionStatus.ANALYSIS),
        decision=_decision(),
        risk_eval=_risk_eval(),
    )

    assert order is None
    assert not store.orders
    assert not store.positions


async def test_blocked_decision_does_not_create_order():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)

    order = await _wire_paper_decision(
        broker,
        result=_result(status=DecisionStatus.BLOCKED),
        decision=_decision(),
        risk_eval=_risk_eval(),
    )

    assert order is None
    assert not store.orders
    assert not store.positions


async def test_no_broker_gateway_is_a_noop():
    order = await _wire_paper_decision(
        None, result=_result(), decision=_decision(), risk_eval=_risk_eval()
    )
    assert order is None


# ---------------------------------------------------------------------------
# Idempotency / replay safety.
# ---------------------------------------------------------------------------


async def test_replayed_decision_created_false_does_not_create_order():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)

    order = await _wire_paper_decision(
        broker,
        result=_result(created=False),
        decision=_decision(),
        risk_eval=_risk_eval(),
    )

    assert order is None
    assert not store.orders
    assert not store.positions


async def test_same_decision_id_never_creates_second_order():
    """A redelivered decision never persists a second pending order."""
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    decision = _decision()

    first = await _wire_paper_decision(
        broker, result=_result(), decision=decision, risk_eval=_risk_eval()
    )
    assert first is not None
    assert len(store.orders) == 1

    # Same decision, redelivered: the in-memory one-order-per-decision guard
    # rejects the duplicate (uq_orders_paper_decision_id is the DB backstop).
    second = await _wire_paper_decision(
        broker, result=_result(), decision=decision, risk_eval=_risk_eval()
    )
    assert second is None
    assert len(store.orders) == 1
    assert sum(1 for p in store.positions if p.status == PaperPositionStatus.OPEN.value) == 0


# ---------------------------------------------------------------------------
# Broker rejection (clean None) and broker failure (propagates => rollback).
# ---------------------------------------------------------------------------


async def test_ledger_broker_rejection_returns_none_for_flat_direction():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)

    order = await _wire_paper_decision(
        broker,
        result=_result(direction=Direction.FLAT),
        decision=_decision(),
        risk_eval=_risk_eval(),
    )

    assert order is None
    assert not store.orders
    assert not store.positions


async def test_ledger_broker_rejection_returns_none_when_same_side_open():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    decision = _decision()

    order = await _wire_paper_decision(
        broker, result=_result(), decision=decision, risk_eval=_risk_eval()
    )
    assert order is not None
    await broker.fill_pending(order, open_price=1.10, ts=_NOW)
    assert await store.get_open_position("EURUSD") is not None

    second = await _wire_paper_decision(
        broker,
        result=_result(direction=Direction.LONG),  # same side: keep-policy
        decision=_decision(),
        risk_eval=_risk_eval(),
    )
    assert second is None
    assert len(store.orders) == 1  # the rejection added nothing


async def test_opposing_open_position_submits_a_pending_order():
    """An opposing open position still submits; the lifecycle flips next-bar."""
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    decision = _decision()

    order = await _wire_paper_decision(
        broker, result=_result(), decision=decision, risk_eval=_risk_eval()
    )
    assert order is not None
    await broker.fill_pending(order, open_price=1.10, ts=_NOW)

    opposing = await _wire_paper_decision(
        broker,
        result=_result(direction=Direction.SHORT),
        decision=_decision(),
        risk_eval=_risk_eval(),
    )
    assert opposing is not None
    assert opposing.status == PaperOrderStatus.PENDING.value  # flip at next open
    assert len(store.orders) == 2


async def test_missing_sizing_never_opens_a_position():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)
    risk_eval = _risk_eval(units="0.000000")  # risk gate produced no sizing

    order = await _wire_paper_decision(
        broker, result=_result(), decision=_decision(), risk_eval=risk_eval
    )

    assert order is None
    assert not store.orders
    assert not store.positions


async def test_broker_failure_propagates_for_transaction_rollback():
    """A broker crash must surface so the caller rolls back everything."""
    store = InMemoryStore()
    broker = _RaisingBroker()

    with pytest.raises(RuntimeError, match="simulated broker failure"):
        await _wire_paper_decision(
            broker, result=_result(), decision=_decision(), risk_eval=_risk_eval()
        )
    assert not store.orders
    assert not store.positions


async def test_lost_decision_row_or_risk_row_is_a_noop():
    store = InMemoryStore()
    broker = LedgerBroker(store=store, seed=7)

    assert (
        await _wire_paper_decision(broker, result=_result(), decision=None, risk_eval=_risk_eval())
        is None
    )
    assert (
        await _wire_paper_decision(broker, result=_result(), decision=_decision(), risk_eval=None)
        is None
    )
    assert not store.orders


# ---------------------------------------------------------------------------
# SAFE MODE: the wiring gateway has no live-order shape.
# ---------------------------------------------------------------------------


def test_wiring_gateway_exposes_no_live_order_methods():
    names = {m for m in dir(PaperBrokerGateway)}
    assert "submit_paper_order" in names
    for bad in ("submit_order", "route_order", "place_order", "create_live_order"):
        assert bad not in names


def test_orchestrator_wiring_has_no_live_execution_imports():
    """Structural: the wiring module must not import any live-execution surface."""
    import ast
    import inspect

    import app.workers.orchestrator_worker as mod

    tree = ast.parse(inspect.getsource(mod))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)

    forbidden = {"app.execution", "app.orders", "app.api", "app.services"}
    assert forbidden.isdisjoint(imports)
    live = [i for i in imports if any(tok in i.lower() for tok in ("oanda", "mt5", "fix", "live"))]
    assert not live
