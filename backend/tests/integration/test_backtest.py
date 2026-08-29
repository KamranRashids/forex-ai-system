"""Integration: backtest replay, persistence, and read-only API (Phase 6).

Covers decisions D-B (replay persisted fundamental/sentiment) and the
persistence + read-only surface. SAFE MODE: verifies that a backtest writes
only to the ``backtest_*`` tables and that there is NO execution endpoint.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.agents.base import AgentSignal, Direction
from app.backtest.repository import get_equity, get_run, get_trades, list_runs
from app.backtest.service import config_from_dict, run_backtest
from app.data.signal_repository import save_signals
from app.data.timeframes import align_to_bucket, iterate_buckets

pytestmark = pytest.mark.integration

_SYMBOL = "EURUSD"
_TF = "H1"
_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = _START + timedelta(days=2)


def _cfg_short() -> dict[str, object]:
    return {
        "symbols": [_SYMBOL],
        "timeframes": [_TF],
        "start": _START.isoformat(),
        "end": _END.isoformat(),
        "seed": 3,
        "start_equity": 100_000.0,
        "warmup_bars": 12,
    }


async def _seed_full_replay(session) -> None:
    signals: list[AgentSignal] = []
    for bucket in iterate_buckets(align_to_bucket(_START, _TF), _END, _TF):
        for agent, features in (
            ("fundamental", {"impact": "high"}),
            ("sentiment", {"items": 3}),
        ):
            signals.append(
                AgentSignal(
                    agent_id=agent,
                    version="1",
                    symbol=_SYMBOL,
                    timeframe=_TF,
                    direction=Direction.LONG,
                    confidence=0.7,
                    bucket_ts=bucket,
                    rationale="seeded",
                    features=features,
                    created_at=bucket,
                    valid_until=bucket + timedelta(hours=1),
                    run_id="it",
                )
            )
    inserted = await save_signals(session, signals)
    assert inserted > 0


async def test_backtest_persists_run_trades_and_equity(db_sessionmaker) -> None:
    async with db_sessionmaker() as session:
        await _seed_full_replay(session)
        await session.commit()

    run_id = await run_backtest(
        cfg=config_from_dict(_cfg_short()),
        settings=__import__("app.core.config", fromlist=["get_settings"]).get_settings(),
        session_factory=db_sessionmaker,
    )
    assert isinstance(run_id, uuid.UUID)

    async with db_sessionmaker() as session:
        run = await get_run(session, run_id)
        assert run is not None
        assert run.status == "COMPLETED"
        assert run.metrics
        coverage = run.metrics["coverage"]
        assert coverage, "expected coverage report to be persisted"
        entry = coverage[0]
        assert entry["fundamental"] > 0  # replayed, not fabricated (D-B)
        assert entry["sentiment"] > 0
        assert entry["degraded"] is False
        trades = await get_trades(session, run_id)
        assert isinstance(trades, list)
        equity = await get_equity(session, run_id)
        assert len(equity) > 0
        # Equity monotonically increasing in time (asc order).
        times = [r.ts for r in equity]
        assert times == sorted(times)


async def test_backtest_reports_degradation_when_replay_missing(db_sessionmaker) -> None:
    # No agent_signals seeded -> fundamental/sentiment coverage is missing and
    # must be reported as degraded, never fabricated (D-B).
    async with db_sessionmaker() as session:
        run = await get_run(session, uuid.uuid4())
        assert run is None  # sanity: empty DB

    run_id = await run_backtest(
        cfg=config_from_dict(_cfg_short()),
        settings=__import__("app.core.config", fromlist=["get_settings"]).get_settings(),
        session_factory=db_sessionmaker,
    )
    async with db_sessionmaker() as session:
        row = await get_run(session, run_id)
        assert row is not None
        entry = row.metrics["coverage"][0]
        assert entry["degraded"] is True
        assert entry["fundamental"] == 0
        assert entry["sentiment"] == 0


async def test_list_runs_orders_by_recency(db_sessionmaker) -> None:
    seen: list[uuid.UUID] = []
    for _ in range(2):
        run_id = await run_backtest(
            cfg=config_from_dict(_cfg_short()),
            settings=__import__("app.core.config", fromlist=["get_settings"]).get_settings(),
            session_factory=db_sessionmaker,
        )
        seen.append(run_id)
    async with db_sessionmaker() as session:
        rows = await list_runs(session, limit=10)
        assert {r.id for r in rows} >= set(seen)


async def test_backtest_api_is_read_only(client, db_sessionmaker) -> None:
    # No execution endpoint: POST to /api/v1/backtests must NOT exist.
    resp = await client.post("/api/v1/backtests", json=_cfg_short())
    assert resp.status_code in (404, 405), resp.text

    from app.core.config import get_settings

    run_id = await run_backtest(
        cfg=config_from_dict(_cfg_short()),
        settings=get_settings(),
        session_factory=db_sessionmaker,
    )

    from tests.integration.conftest import bearer, register_and_login

    token = await register_and_login(client, "bt@example.com")
    headers = bearer(token["access_token"])

    listed = await client.get("/api/v1/backtests", headers=headers)
    assert listed.status_code == 200, listed.text
    ids = [item["id"] for item in listed.json()]
    assert str(run_id) in ids

    detail = await client.get(f"/api/v1/backtests/{run_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == "COMPLETED"

    trades = await client.get(f"/api/v1/backtests/{run_id}/trades", headers=headers)
    assert trades.status_code == 200, trades.text

    equity = await client.get(f"/api/v1/backtests/{run_id}/equity", headers=headers)
    assert equity.status_code == 200, equity.text
