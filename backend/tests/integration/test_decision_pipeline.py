"""Integration: decision pipeline over real PostgreSQL + fakeredis streams.

Flow under test (Phase 5): persisted agent signals -> DecisionEngine/Orchestrator
fuse -> risk-gate -> persist decision + risk evaluation. Verifies:
- engine produces ANALYSIS / PAPER / BLOCKED only (no live shapes),
- decisions are idempotent (replay never duplicates),
- orchestrator trigger consumes signal.emitted and emits decision.emitted,
- decisions + risk read APIs and RBAC,
- admin risk-params and replay APIs with fail-closed clamping.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

pytestmark = [pytest.mark.integration]

PASSWORD = "correct-horse-battery-staple"
SYMBOL = "EURUSD"
TF = "M15"
# Most recent closed M15 bucket (computed once at import) so decisions are
# "fresh" for the API's fresh_only filter against datetime.now(UTC).
from app.data.timeframes import previous_closed_bucket  # noqa: E402

_BUCKET = previous_closed_bucket(datetime.now(UTC), "M15")
_NOW = _BUCKET + timedelta(minutes=15)


async def _seed_decision_inputs(
    db_sessionmaker: Any, *, tech_direction: str = "LONG", atr14: float = 0.005, close: float = 1.1
) -> None:
    """Insert an instrument, one candle (for price) and 4 deterministic signals."""
    from app.data.ingest import seed_instruments
    from app.models.agent_signal import AgentSignalRow
    from app.models.candle import CandleRow

    async with db_sessionmaker() as session:
        inst = (await seed_instruments(session, [SYMBOL]))[SYMBOL]
        session.add(
            CandleRow(
                instrument_id=inst.id,
                timeframe=TF,
                ts=_BUCKET,
                open=Decimal(str(close)),
                high=Decimal(str(close)),
                low=Decimal(str(close)),
                close=Decimal(str(close)),
                volume=100,
                source="synthetic",
                complete=True,
                tf_minutes=15,
            )
        )
        session.add_all(
            [
                AgentSignalRow(
                    agent_id="technical",
                    agent_version="1",
                    symbol=SYMBOL,
                    timeframe=TF,
                    direction=tech_direction,
                    confidence=Decimal("0.8"),
                    bucket_ts=_BUCKET,
                    features={"atr14": atr14, "regime": "trending"},
                    rationale="t",
                ),
                AgentSignalRow(
                    agent_id="regime",
                    agent_version="1",
                    symbol=SYMBOL,
                    timeframe=TF,
                    direction="FLAT",
                    confidence=Decimal("0.0"),
                    bucket_ts=_BUCKET,
                    features={"regime": "trending"},
                    rationale="r",
                ),
                AgentSignalRow(
                    agent_id="fundamental",
                    agent_version="1",
                    symbol=SYMBOL,
                    timeframe=TF,
                    direction="LONG",
                    confidence=Decimal("0.7"),
                    bucket_ts=_BUCKET,
                    features={},
                    rationale="f",
                ),
                AgentSignalRow(
                    agent_id="sentiment",
                    agent_version="1",
                    symbol=SYMBOL,
                    timeframe=TF,
                    direction="LONG",
                    confidence=Decimal("0.7"),
                    bucket_ts=_BUCKET,
                    features={},
                    rationale="s",
                ),
            ]
        )
        await session.commit()


async def _count_decisions(db_sessionmaker: Any) -> int:
    from app.models.decision import DecisionRow

    async with db_sessionmaker() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(DecisionRow).where(DecisionRow.symbol == SYMBOL)
            )
        )


async def _config_pair(db_sessionmaker: Any, *, actor: str = "admin@example.com") -> None:
    from app.data.market_config import set_market_config

    async with db_sessionmaker() as session:
        await set_market_config(session, actor=actor, symbols=[SYMBOL], timeframes=[TF])
        await session.commit()


async def _run_engine(db_sessionmaker: Any) -> Any:
    """Run the DB-aware decision engine once; returns the DecideResult."""
    from app.core.config import get_settings
    from app.data.risk_config import load_risk_params
    from app.decisions.engine import DecisionEngine, OrchParams

    async with db_sessionmaker() as session:
        engine = DecisionEngine(session=session, now=_NOW)
        risk = await load_risk_params(session, get_settings())
        result = await engine.decide(
            symbol=SYMBOL,
            timeframe=TF,
            configured=[TF],
            crafts=OrchParams(),
            risk=risk,
        )
        await session.commit()
    return result


def _orch_worker(db_sessionmaker: Any, fake_redis: Any) -> Any:
    from app.bus.publisher import RedisEventPublisher
    from app.workers.orchestrator_worker import OrchestratorWorker

    return OrchestratorWorker(
        session_factory=db_sessionmaker,
        redis=fake_redis,
        publisher=RedisEventPublisher(fake_redis, producer_name="orchestrator"),
    )


async def _role_headers(
    client: httpx.AsyncClient, db_sessionmaker: Any, email: str, role: str
) -> dict[str, str]:
    """Register a user via API and set their role explicitly (deterministic)."""
    from tests.integration.conftest import bearer, register_and_login

    tokens = await register_and_login(client, email, PASSWORD)
    from app.models.user import User
    from sqlalchemy import update

    async with db_sessionmaker() as session:
        await session.execute(update(User).where(User.email == email).values(role=role))
        await session.commit()
    return bearer(tokens["access_token"])


# ---------------------------------------------------------------------------
# Core pipeline: engine + persistence + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_persists_paper_decision_and_risk_eval(db_sessionmaker: Any) -> None:
    from app.models.decision import DecisionStatus

    await _seed_decision_inputs(db_sessionmaker)
    result = await _run_engine(db_sessionmaker)

    assert result.symbol == SYMBOL
    assert result.action.value == "PERSIST"
    assert result.status in (DecisionStatus.ANALYSIS, DecisionStatus.PAPER, DecisionStatus.BLOCKED)
    assert result.created is True
    assert len(result.inputs_hash) == 64

    # All four directional/voter inputs present -> full coverage -> PAPER.
    assert result.status == DecisionStatus.PAPER
    assert result.direction.value in ("LONG", "SHORT", "FLAT")
    assert result.veto_code is None

    from app.models.decision import DecisionRow
    from app.models.risk_evaluation import RiskEvaluationRow

    async with db_sessionmaker() as session:
        decision = (
            (
                await session.execute(
                    select(DecisionRow).where(
                        DecisionRow.symbol == SYMBOL, DecisionRow.timeframe == TF
                    )
                )
            )
            .scalars()
            .first()
        )
        risk = (
            (
                await session.execute(
                    select(RiskEvaluationRow).where(
                        RiskEvaluationRow.symbol == SYMBOL, RiskEvaluationRow.timeframe == TF
                    )
                )
            )
            .scalars()
            .first()
        )

    assert decision is not None
    assert decision.status == "PAPER"
    assert decision.inputs_hash == result.inputs_hash
    assert decision.valid_until is not None

    assert risk is not None
    assert risk.passed is True
    assert risk.exposure_ok is True
    assert risk.correlation_ok is True
    assert risk.daily_loss_ok is True
    assert risk.drawdown_ok is True
    assert risk.position_size_units is not None
    assert risk.price is not None
    assert risk.atr is not None
    assert risk.stop_loss is not None
    assert risk.take_profit is not None


@pytest.mark.asyncio
async def test_replay_is_idempotent_no_duplicate_decisions(db_sessionmaker: Any) -> None:
    await _seed_decision_inputs(db_sessionmaker)
    first = await _run_engine(db_sessionmaker)
    assert first.created is True
    assert await _count_decisions(db_sessionmaker) == 1

    second = await _run_engine(db_sessionmaker)
    assert second.created is False
    assert await _count_decisions(db_sessionmaker) == 1


@pytest.mark.asyncio
async def test_missing_price_fails_closed_to_blocked(db_sessionmaker: Any) -> None:
    # No candle -> price is None -> risk fails closed (BLOCKED), not PAPER.
    from app.core.config import get_settings
    from app.data.risk_config import load_risk_params
    from app.decisions.engine import DecisionEngine, OrchParams
    from app.models.agent_signal import AgentSignalRow
    from app.models.decision import DecisionStatus

    async with db_sessionmaker() as session:
        session.add_all(
            [
                AgentSignalRow(
                    agent_id="technical",
                    agent_version="1",
                    symbol=SYMBOL,
                    timeframe=TF,
                    direction="LONG",
                    confidence=Decimal("0.8"),
                    bucket_ts=_BUCKET,
                    features={"atr14": 0.005, "regime": "trending"},
                    rationale="t",
                ),
                AgentSignalRow(
                    agent_id="regime",
                    agent_version="1",
                    symbol=SYMBOL,
                    timeframe=TF,
                    direction="FLAT",
                    confidence=Decimal("0.0"),
                    bucket_ts=_BUCKET,
                    features={"regime": "trending"},
                    rationale="r",
                ),
                AgentSignalRow(
                    agent_id="fundamental",
                    agent_version="1",
                    symbol=SYMBOL,
                    timeframe=TF,
                    direction="LONG",
                    confidence=Decimal("0.7"),
                    bucket_ts=_BUCKET,
                    features={},
                    rationale="f",
                ),
                AgentSignalRow(
                    agent_id="sentiment",
                    agent_version="1",
                    symbol=SYMBOL,
                    timeframe=TF,
                    direction="LONG",
                    confidence=Decimal("0.7"),
                    bucket_ts=_BUCKET,
                    features={},
                    rationale="s",
                ),
            ]
        )
        await session.commit()

        engine = DecisionEngine(session=session, now=_NOW)
        risk = await load_risk_params(session, get_settings())
        result = await engine.decide(
            symbol=SYMBOL, timeframe=TF, configured=[TF], crafts=OrchParams(), risk=risk
        )
        await session.commit()

    # No instrument/candle -> price unavailable -> risk blocks (never PAPER).
    assert result.status == DecisionStatus.BLOCKED
    assert result.veto_code is not None


# ---------------------------------------------------------------------------
# Orchestrator worker: trigger consume + decision.emitted
# ---------------------------------------------------------------------------


async def _emit_signal_trigger(fake_redis: Any, symbol: str, timeframe: str) -> None:
    from app.bus.events import Event

    event = Event(
        event_type="signal.emitted",
        payload={"agent_id": "technical", "symbol": symbol, "timeframe": timeframe},
        producer="agents",
        produced_at=datetime.now(UTC),
    )
    await fake_redis.xadd("signals.stream", {"data": event.to_json()})


@pytest.mark.asyncio
async def test_orchestrator_consumes_trigger_and_emits_decision(
    db_sessionmaker: Any, fake_redis: Any
) -> None:
    await _seed_decision_inputs(db_sessionmaker)
    await _config_pair(db_sessionmaker, actor="admin@example.com")
    worker = _orch_worker(db_sessionmaker, fake_redis)
    await worker.ensure_groups()

    await _emit_signal_trigger(fake_redis, SYMBOL, TF)
    batch = await worker.poll_once()

    assert batch.errors == 0
    assert batch.processed >= 1
    assert await _count_decisions(db_sessionmaker) == 1

    entries = await fake_redis.xrange("decisions.stream")
    emitted: list[dict[str, Any]] = []
    for _entry_id, fields in entries:
        raw = fields.get("data", fields.get(b"data"))
        envelope = json.loads(raw)
        if envelope["event_type"] == "decision.emitted":
            emitted.append(envelope["payload"])
    assert emitted, "no decision.emitted published"
    assert emitted[0]["status"] in ("ANALYSIS", "PAPER", "BLOCKED")


@pytest.mark.asyncio
async def test_orchestrator_replay_trigger_is_idempotent(
    db_sessionmaker: Any, fake_redis: Any
) -> None:
    await _seed_decision_inputs(db_sessionmaker)
    await _config_pair(db_sessionmaker, actor="admin@example.com")
    worker = _orch_worker(db_sessionmaker, fake_redis)
    await worker.ensure_groups()

    await _emit_signal_trigger(fake_redis, SYMBOL, TF)
    first = await worker.poll_once()
    assert first.processed >= 1
    assert await _count_decisions(db_sessionmaker) == 1

    # Re-emit the same trigger; the second pass must be idempotent.
    await _emit_signal_trigger(fake_redis, SYMBOL, TF)
    second = await worker.poll_once()
    assert second.errors == 0
    assert await _count_decisions(db_sessionmaker) == 1


# ---------------------------------------------------------------------------
# Decisions + risk read APIs and RBAC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decisions_api_read_and_rbac(client: httpx.AsyncClient, db_sessionmaker: Any) -> None:

    await _seed_decision_inputs(db_sessionmaker)
    await _run_engine(db_sessionmaker)

    headers = await _role_headers(client, db_sessionmaker, "decision-viewer@example.com", "viewer")

    latest = await client.get(
        "/api/v1/decisions/latest",
        params={"symbol": SYMBOL, "timeframe": TF},
        headers=headers,
    )
    assert latest.status_code == 200
    body = latest.json()
    assert body["status"] in ("ANALYSIS", "PAPER", "BLOCKED")
    assert body["fused_direction"] in ("LONG", "SHORT", "FLAT")
    assert len(body["inputs_hash"]) == 64
    assert body["weights"] != {}

    history = await client.get(
        "/api/v1/decisions",
        params={"symbol": SYMBOL, "timeframe": TF},
        headers=headers,
    )
    assert history.status_code == 200
    assert len(history.json()) == 1

    evals = await client.get(
        "/api/v1/decisions/risk-evaluations",
        params={"symbol": SYMBOL, "timeframe": TF},
        headers=headers,
    )
    assert evals.status_code == 200
    evals_body = evals.json()
    assert len(evals_body) == 1
    assert evals_body[0]["passed"] is True
    assert evals_body[0]["position_size_units"] is not None

    anon = await client.get(
        "/api/v1/decisions/latest",
        params={"symbol": SYMBOL, "timeframe": TF},
    )
    assert anon.status_code == 401


@pytest.mark.asyncio
async def test_risk_state_api_admin_only(client: httpx.AsyncClient, db_sessionmaker: Any) -> None:

    await _seed_decision_inputs(db_sessionmaker)
    await _run_engine(db_sessionmaker)

    await _config_pair(db_sessionmaker, actor="admin@example.com")
    # Re-run engine so _refresh_exposure writes risk_state rows.
    await _run_engine(db_sessionmaker)

    viewer = await _role_headers(client, db_sessionmaker, "risk-viewer@example.com", "viewer")
    admin = await _role_headers(client, db_sessionmaker, "risk-admin@example.com", "admin")

    denied = await client.get("/api/v1/risk/state", headers=viewer)
    assert denied.status_code == 403

    ok = await client.get("/api/v1/risk/state", headers=admin)
    assert ok.status_code == 200
    payload = ok.json()
    assert "account" in payload and "daily" in payload
    assert payload["account"]["exposure"] >= 0.0


@pytest.mark.asyncio
async def test_risk_params_api_clamps_out_of_bounds(
    client: httpx.AsyncClient, db_sessionmaker: Any
) -> None:
    viewer = await _role_headers(client, db_sessionmaker, "params-viewer@example.com", "viewer")
    admin = await _role_headers(client, db_sessionmaker, "params-admin@example.com", "admin")

    denied = await client.get("/api/v1/risk/params", headers=viewer)
    assert denied.status_code == 403

    current = await client.get("/api/v1/risk/params", headers=admin)
    assert current.status_code == 200
    original_max_exposure = current.json()["max_exposure_pct"]

    # Out-of-bounds override (1.2 > 1.0) is clamped back to the env default.
    updated = await client.put(
        "/api/v1/risk/params",
        json={"max_exposure_pct": 1.2, "max_risk_pct_account": 0.02},
        headers=admin,
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["max_risk_pct_account"] == 0.02  # valid override kept
    assert body["max_exposure_pct"] <= 1.0  # out-of-bounds clamped/fail-closed

    # Read-back reflects the stored override for the editable key.
    recheck = await client.get("/api/v1/risk/params", headers=admin)
    assert recheck.json()["max_risk_pct_account"] == 0.02

    # Restore to avoid cross-test contamination.
    await client.put(
        "/api/v1/risk/params",
        json={"max_exposure_pct": original_max_exposure, "max_risk_pct_account": 0.01},
        headers=admin,
    )


@pytest.mark.asyncio
async def test_admin_replay_endpoint(
    client: httpx.AsyncClient, db_sessionmaker: Any, fake_redis: Any
) -> None:
    viewer = await _role_headers(client, db_sessionmaker, "replay-viewer@example.com", "viewer")
    admin = await _role_headers(client, db_sessionmaker, "replay-admin@example.com", "admin")
    await _config_pair(db_sessionmaker, actor="admin@example.com")

    denied = await client.post(
        "/api/v1/admin/decisions/replay",
        json={"symbol": SYMBOL, "timeframe": TF},
        headers=viewer,
    )
    assert denied.status_code == 403

    accepted = await client.post(
        "/api/v1/admin/decisions/replay",
        json={"symbol": SYMBOL, "timeframe": TF},
        headers=admin,
    )
    assert accepted.status_code == 202
    assert accepted.json()["symbol"] == SYMBOL

    # A trigger was queued for the orchestrator consumer group.
    entries = await fake_redis.xrange("signals.stream")
    assert entries
    raw = entries[-1][1].get("data", entries[-1][1].get(b"data"))
    envelope = json.loads(raw)
    assert envelope["event_type"] == "signal.emitted"
    assert envelope["payload"]["symbol"] == SYMBOL
