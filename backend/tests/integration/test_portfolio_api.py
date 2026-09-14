"""Integration: read-only portfolio API over the Phase A ledger (Phase 13, Phase B).

Seeds paper orders/positions/account snapshots (plus one decision so the
``decision_id`` join is real), then verifies the four GET endpoints: shapes,
chronological ordering, status filters, limit clamping, RBAC (anonymous 401,
viewer+ 200) and that every read is side-effect free (row counts unchanged).

SAFE MODE is preserved: these endpoints expose paper-accounting records only;
no write endpoint exists anywhere in the portfolio router.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.models.decision import DecisionRow
from app.models.paper_ledger import (
    AccountSnapshotRow,
    PaperOrderRow,
    PaperOrderStatus,
    PaperPositionRow,
    PaperPositionStatus,
)
from sqlalchemy import func, select
from tests.integration.conftest import bearer, register_and_login

pytestmark = pytest.mark.integration

_T0 = datetime(2024, 3, 1, tzinfo=UTC)
_T1 = _T0 + timedelta(hours=1)
_T2 = _T0 + timedelta(hours=2)

PASSWORD = "correct-horse-battery-staple"


def _ts(value: str) -> datetime:
    """Parse a JSON datetime robustly (pydantic may emit 'Z' or '+00:00')."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _seed(db_sessionmaker) -> dict[str, object]:
    """Insert a decision, one FILLED order + OPEN position, 3 snapshots, 1 PENDING order."""
    async with db_sessionmaker() as session:
        decision = DecisionRow(
            run_id="run-portfolio",
            symbol="EURUSD",
            timeframe="H1",
            bucket_ts=_T0,
            fused_direction="LONG",
            confidence=Decimal("0.8000"),
            agreement=Decimal("0.7000"),
            status="PAPER",
            inputs_hash="h" * 64,
            weights={"technical": 1.0},
            code_versions={"fusion": "1.0.0"},
        )
        session.add(decision)
        await session.flush()

        filled = PaperOrderRow(
            decision_id=decision.id,
            symbol="EURUSD",
            timeframe="H1",
            side="LONG",
            order_type="NEXT_OPEN",
            status=PaperOrderStatus.FILLED.value,
            units=Decimal("10000"),
            requested_price=Decimal("1.0850"),
            filled_price=Decimal("1.0850"),
            costs=Decimal("0.155000"),
            seed=0,
            filled_at=_T0,
        )
        session.add(filled)
        await session.flush()

        session.add(
            PaperPositionRow(
                order_id=filled.id,
                symbol="EURUSD",
                timeframe="H1",
                side="LONG",
                units=Decimal("10000"),
                entry_price=Decimal("1.0850"),
                entry_ts=_T0,
                stop_loss=Decimal("1.0750"),
                take_profit=Decimal("1.0950"),
                costs=Decimal("0.155000"),
                status=PaperPositionStatus.OPEN.value,
            )
        )

        for i, ts in enumerate((_T0, _T1, _T2)):
            session.add(
                AccountSnapshotRow(
                    ts=ts,
                    cash=Decimal("99999.845000"),
                    equity=Decimal(f"{100000 + i * 100}.000000"),
                    open_pnl=Decimal("0.155000"),
                    realized_pnl=Decimal("0.000000"),
                    peak_equity=Decimal("100200.000000"),
                    drawdown_pct=Decimal("0.00000000"),
                    margin_used=Decimal("0.000000"),
                    extra={},
                )
            )

        session.add(
            PaperOrderRow(
                symbol="GBPUSD",
                timeframe="H1",
                side="SHORT",
                order_type="NEXT_OPEN",
                status=PaperOrderStatus.PENDING.value,
                units=Decimal("5000"),
            )
        )
        await session.commit()
    return {"decision_id": decision.id}


async def _viewer(client, email: str) -> dict[str, str]:
    tokens = await register_and_login(client, email, PASSWORD)
    return bearer(tokens["access_token"])


async def test_anonymous_gets_401_on_all_portfolio_endpoints(client) -> None:
    for path in (
        "/api/v1/portfolio/summary",
        "/api/v1/portfolio/positions",
        "/api/v1/portfolio/orders",
        "/api/v1/portfolio/equity",
    ):
        resp = await client.get(path)
        assert resp.status_code == 401


async def test_summary_empty_ledger_returns_zeros(client) -> None:
    headers = await _viewer(client, "viewer-empty@example.com")
    resp = await client.get("/api/v1/portfolio/summary", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["as_of"] is None
    assert body["equity"] == 0.0
    assert body["cash"] == 0.0
    assert body["open_positions"] == 0
    assert body["open_orders"] == 0


async def test_viewer_reads_portfolio_summary(client, db_sessionmaker) -> None:
    await _seed(db_sessionmaker)
    headers = await _viewer(client, "viewer-summary@example.com")

    resp = await client.get("/api/v1/portfolio/summary", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert _ts(body["as_of"]) == _T2
    assert body["equity"] == pytest.approx(100200.0, abs=1e-6)
    assert body["cash"] == pytest.approx(99999.845, abs=1e-6)
    assert body["open_positions"] == 1
    assert body["open_orders"] == 1
    assert body["drawdown_pct"] == pytest.approx(0.0, abs=1e-9)


async def test_positions_report_decision_id_and_status_filter(client, db_sessionmaker) -> None:
    seeded = await _seed(db_sessionmaker)
    headers = await _viewer(client, "viewer-positions@example.com")

    resp = await client.get("/api/v1/portfolio/positions", headers=headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "EURUSD"
    assert row["timeframe"] == "H1"
    assert row["side"] == "LONG"
    assert row["status"] == "OPEN"
    assert row["decision_id"] == str(seeded["decision_id"])
    assert row["entry_price"] == pytest.approx(1.0850, abs=1e-6)
    assert row["units"] == pytest.approx(10000.0, abs=1e-6)
    assert row["entry_ts"]

    closed = await client.get("/api/v1/portfolio/positions?status=CLOSED", headers=headers)
    assert closed.status_code == 200
    assert closed.json() == []

    bad = await client.get("/api/v1/portfolio/positions?status=MIA", headers=headers)
    assert bad.status_code == 422
    zero = await client.get("/api/v1/portfolio/positions?limit=0", headers=headers)
    assert zero.status_code == 422


async def test_orders_filters_and_limit(client, db_sessionmaker) -> None:
    await _seed(db_sessionmaker)
    headers = await _viewer(client, "viewer-orders@example.com")

    all_orders = await client.get("/api/v1/portfolio/orders", headers=headers)
    assert all_orders.status_code == 200
    assert len(all_orders.json()) == 2

    filled = await client.get("/api/v1/portfolio/orders?status=FILLED", headers=headers)
    assert filled.status_code == 200
    assert len(filled.json()) == 1
    assert filled.json()[0]["status"] == "FILLED"
    assert filled.json()[0]["symbol"] == "EURUSD"

    pending = await client.get("/api/v1/portfolio/orders?status=PENDING", headers=headers)
    assert pending.status_code == 200
    assert len(pending.json()) == 1
    assert pending.json()[0]["decision_id"] is None

    limited = await client.get("/api/v1/portfolio/orders?limit=1", headers=headers)
    assert limited.status_code == 200
    assert len(limited.json()) == 1

    wrong_pool = await client.get("/api/v1/portfolio/orders?status=OPEN", headers=headers)
    assert wrong_pool.status_code == 422


async def test_equity_history_chronological_and_limit_clamp(client, db_sessionmaker) -> None:
    await _seed(db_sessionmaker)
    headers = await _viewer(client, "viewer-equity@example.com")

    resp = await client.get("/api/v1/portfolio/equity", headers=headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert [_ts(r["ts"]) for r in rows] == [_T0, _T1, _T2]
    assert rows[2]["equity"] == pytest.approx(100200.0, abs=1e-6)
    assert rows[0]["cash"] == pytest.approx(99999.845, abs=1e-6)

    two = await client.get("/api/v1/portfolio/equity?limit=2", headers=headers)
    assert [_ts(r["ts"]) for r in two.json()] == [_T1, _T2]

    too_many = await client.get("/api/v1/portfolio/equity?limit=1001", headers=headers)
    assert too_many.status_code == 422
    zero = await client.get("/api/v1/portfolio/equity?limit=0", headers=headers)
    assert zero.status_code == 422


async def test_portfolio_endpoints_are_read_only(client, db_sessionmaker) -> None:
    await _seed(db_sessionmaker)
    headers = await _viewer(client, "viewer-readonly@example.com")

    async with db_sessionmaker() as session:
        before = (
            await session.execute(select(func.count()).select_from(PaperPositionRow))
        ).scalar_one()
        before_orders = (
            await session.execute(select(func.count()).select_from(PaperOrderRow))
        ).scalar_one()
        before_snapshots = (
            await session.execute(select(func.count()).select_from(AccountSnapshotRow))
        ).scalar_one()

    for path in (
        "/api/v1/portfolio/summary",
        "/api/v1/portfolio/positions",
        "/api/v1/portfolio/orders",
        "/api/v1/portfolio/equity",
    ):
        resp = await client.get(path, headers=headers)
        assert resp.status_code == 200

    async with db_sessionmaker() as session:
        after = (
            await session.execute(select(func.count()).select_from(PaperPositionRow))
        ).scalar_one()
        after_orders = (
            await session.execute(select(func.count()).select_from(PaperOrderRow))
        ).scalar_one()
        after_snapshots = (
            await session.execute(select(func.count()).select_from(AccountSnapshotRow))
        ).scalar_one()

    assert (after, after_orders, after_snapshots) == (before, before_orders, before_snapshots)
    assert after == 1  # sanity: the seeded OPEN position is still there
    assert after_snapshots == 3
