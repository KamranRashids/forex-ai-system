"""Unit tests: portfolio_reader read layer over a scripted fake session.

DB-free (unit gate): the reader is exercised through a fake ``AsyncSession``
whose ``execute`` returns scripted rows, so the mapping (Decimal -> float), the
empty-ledger summary and the status validation are verified without
PostgreSQL. Real SQL semantics (joins, ordering, limits) are covered by
tests/integration/test_portfolio_api.py against the scratch database.

(Phase 13, Phase B)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.data.portfolio_reader import (
    list_orders,
    list_positions,
    load_equity_history,
    load_portfolio_summary,
)
from app.models.paper_ledger import (
    AccountSnapshotRow,
    PaperOrderRow,
    PaperOrderStatus,
    PaperPositionRow,
    PaperPositionStatus,
)

_T0 = datetime(2024, 3, 1, tzinfo=UTC)
_T1 = _T0 + timedelta(hours=1)
_T2 = _T0 + timedelta(hours=2)


class _FakeScalars:
    def __init__(self, items: list[object]) -> None:
        self._items = items

    def all(self) -> list[object]:
        return self._items

    def first(self) -> object | None:
        return self._items[0] if self._items else None


class _FakeResult:
    def __init__(self, items: list[object]) -> None:
        self._items = items

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._items)

    def all(self) -> list[object]:
        return self._items

    def scalar_one(self) -> object:
        return self._items[0]


class FakeSession:
    """Scripted session: each execute() returns the next pre-set result batch."""

    def __init__(self, *batches: list[object]) -> None:
        self._script: list[list[object]] = list(batches)
        self.executed: list[object] = []

    async def execute(self, stmt: object) -> _FakeResult:
        self.executed.append(stmt)
        if not self._script:
            raise AssertionError("reader executed more queries than scripted")
        return _FakeResult(self._script.pop(0))

    @property
    def drained(self) -> bool:
        return not self._script


def _snapshot(ts: datetime, equity: Decimal = Decimal("100000.000000")) -> AccountSnapshotRow:
    return AccountSnapshotRow(
        id=uuid.uuid4(),
        ts=ts,
        cash=Decimal("90000.000000"),
        equity=equity,
        open_pnl=Decimal("1000.000000"),
        realized_pnl=Decimal("100.000000"),
        peak_equity=Decimal("101000.000000"),
        drawdown_pct=Decimal("0.00990000"),
        margin_used=Decimal("10000.000000"),
        extra={},
    )


def _order(**overrides: object) -> PaperOrderRow:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "decision_id": None,
        "symbol": "EURUSD",
        "timeframe": "H1",
        "side": "LONG",
        "order_type": "NEXT_OPEN",
        "status": PaperOrderStatus.PENDING.value,
        "units": Decimal("10000"),
        "requested_price": Decimal("1.0850"),
        "filled_price": None,
        "costs": Decimal("0.155000"),
        "stop_loss": None,
        "take_profit": None,
        "filled_at": None,
        "created_at": _T0,
    }
    values.update(overrides)
    return PaperOrderRow(**values)


def _position(**overrides: object) -> PaperPositionRow:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "order_id": uuid.uuid4(),
        "symbol": "EURUSD",
        "timeframe": "H1",
        "side": "LONG",
        "units": Decimal("5000"),
        "entry_price": Decimal("1.1000"),
        "entry_ts": _T0,
        "stop_loss": Decimal("1.0900"),
        "take_profit": Decimal("1.1200"),
        "costs": Decimal("0.077500"),
        "status": PaperPositionStatus.OPEN.value,
        "exit_price": None,
        "exit_ts": None,
        "exit_reason": None,
        "gross_pnl": None,
        "net_pnl": None,
    }
    values.update(overrides)
    return PaperPositionRow(**values)


async def test_positions_maps_join_decision_and_floats() -> None:
    decision_id = uuid.uuid4()
    pos = _position(entry_price=Decimal("1.1050"), take_profit=Decimal("1.1200"), stop_loss=None)
    session = FakeSession([(pos, decision_id)])

    result = await list_positions(session, limit=100)

    assert session.drained
    assert len(result) == 1
    item = result[0]
    assert item.id == pos.id
    assert item.order_id == pos.order_id
    assert item.decision_id == decision_id
    assert item.symbol == "EURUSD"
    assert item.timeframe == "H1"
    assert item.side == "LONG"
    assert item.status == "OPEN"
    assert item.entry_price == pytest.approx(1.1050, abs=1e-6)
    assert item.take_profit == pytest.approx(1.1200, abs=1e-6)
    assert item.units == pytest.approx(5000.0, abs=1e-6)
    assert item.stop_loss is None
    assert item.exit_price is None
    assert item.net_pnl is None


async def test_positions_with_null_decision_id() -> None:
    pos = _position()
    session = FakeSession([(pos, None)])
    result = await list_positions(session, status="CLOSED", limit=10)
    assert len(result) == 1
    assert result[0].decision_id is None


async def test_positions_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="position status"):
        await list_positions(FakeSession(), status="BOGUS", limit=10)


async def test_orders_maps_fields_and_null_decision() -> None:
    order = _order(
        status=PaperOrderStatus.FILLED.value,
        units=Decimal("10000"),
        filled_price=Decimal("1.0850"),
        filled_at=_T1,
        created_at=_T2,
    )
    session = FakeSession([order])

    result = await list_orders(session, limit=50)

    assert session.drained
    assert len(result) == 1
    item = result[0]
    assert item.id == order.id
    assert item.decision_id is None
    assert item.symbol == "EURUSD"
    assert item.side == "LONG"
    assert item.order_type == "NEXT_OPEN"
    assert item.status == "FILLED"
    assert item.units == pytest.approx(10000.0, abs=1e-6)
    assert item.requested_price == pytest.approx(1.0850, abs=1e-6)
    assert item.filled_price == pytest.approx(1.0850, abs=1e-6)
    assert item.costs == pytest.approx(0.155, abs=1e-6)
    assert item.filled_at == _T1
    assert item.created_at == _T2


async def test_orders_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="order status"):
        await list_orders(FakeSession(), status="MIA", limit=10)


async def test_equity_history_returns_chronological_most_recent() -> None:
    newest = _snapshot(_T2, equity=Decimal("100200.000000"))
    mid = _snapshot(_T1, equity=Decimal("100100.000000"))
    oldest = _snapshot(_T0, equity=Decimal("100000.000000"))
    session = FakeSession([newest, mid, oldest])

    result = await load_equity_history(session, limit=500)

    assert session.drained
    assert [p.ts for p in result] == [_T0, _T1, _T2]
    assert result[0].equity == pytest.approx(100000.0, abs=1e-6)
    assert result[2].equity == pytest.approx(100200.0, abs=1e-6)
    assert result[0].drawdown_pct == pytest.approx(0.0099, abs=1e-6)
    assert result[0].margin_used == pytest.approx(10000.0, abs=1e-6)


async def test_equity_history_empty() -> None:
    session = FakeSession([])
    result = await load_equity_history(session, limit=500)
    assert session.drained
    assert result == []


async def test_summary_empty_ledger_returns_zeros() -> None:
    session = FakeSession([])
    summary = await load_portfolio_summary(session)
    assert session.drained
    assert summary.as_of is None
    assert summary.cash == 0.0
    assert summary.equity == 0.0
    assert summary.open_pnl == 0.0
    assert summary.realized_pnl == 0.0
    assert summary.peak_equity == 0.0
    assert summary.drawdown_pct == 0.0
    assert summary.margin_used == 0.0
    assert summary.open_positions == 0
    assert summary.open_orders == 0


async def test_summary_uses_latest_snapshot_and_counts() -> None:
    latest = _snapshot(_T2)
    session = FakeSession([latest], [3], [1])

    summary = await load_portfolio_summary(session)

    assert session.drained
    assert summary.as_of == _T2
    assert summary.equity == pytest.approx(100000.0, abs=1e-6)
    assert summary.cash == pytest.approx(90000.0, abs=1e-6)
    assert summary.open_pnl == pytest.approx(1000.0, abs=1e-6)
    assert summary.realized_pnl == pytest.approx(100.0, abs=1e-6)
    assert summary.peak_equity == pytest.approx(101000.0, abs=1e-6)
    assert summary.drawdown_pct == pytest.approx(0.0099, abs=1e-6)
    assert summary.margin_used == pytest.approx(10000.0, abs=1e-6)
    assert summary.open_positions == 3
    assert summary.open_orders == 1
