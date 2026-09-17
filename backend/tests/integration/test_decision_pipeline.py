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
    db_sessionmaker: Any,
    *,
    tech_direction: str = "LONG",
    fund_direction: str = "LONG",
    senti_direction: str = "LONG",
    tech_conf: str = "0.8",
    fund_conf: str = "0.7",
    senti_conf: str = "0.7",
    atr14: float = 0.005,
    close: float = 1.1,
    with_candle: bool = True,
) -> None:
    """Insert an instrument, a candle (for price) and 4 deterministic signals.

    Defaults reproduce the historical LONG + full-coverage seed that the risk
    gate turns into a PAPER decision. Overriding directions/confidences changes
    the fused outcome deterministically (see the 13C decision-wiring tests).
    """
    from app.data.ingest import seed_instruments
    from app.models.agent_signal import AgentSignalRow
    from app.models.candle import CandleRow

    async with db_sessionmaker() as session:
        inst = (await seed_instruments(session, [SYMBOL]))[SYMBOL]
        if with_candle:
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
                    confidence=Decimal(tech_conf),
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
                    direction=fund_direction,
                    confidence=Decimal(fund_conf),
                    bucket_ts=_BUCKET,
                    features={},
                    rationale="f",
                ),
                AgentSignalRow(
                    agent_id="sentiment",
                    agent_version="1",
                    symbol=SYMBOL,
                    timeframe=TF,
                    direction=senti_direction,
                    confidence=Decimal(senti_conf),
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


def _orch_worker_with_broker(db_sessionmaker: Any, fake_redis: Any) -> Any:
    """Orchestrator wired exactly like production (13C broker seam)."""
    from app.bus.publisher import RedisEventPublisher
    from app.workers.orchestrator_worker import OrchestratorWorker

    def broker_factory(session: Any) -> Any:
        from app.broker.ledger import LedgerBroker
        from app.broker.store import PostgresLedgerStore

        return LedgerBroker(store=PostgresLedgerStore(session=session))

    return OrchestratorWorker(
        session_factory=db_sessionmaker,
        redis=fake_redis,
        publisher=RedisEventPublisher(fake_redis, producer_name="orchestrator"),
        broker_factory=broker_factory,
    )


async def _add_fill_bar_candle(db_sessionmaker: Any, *, close: float = 1.1) -> None:
    """Insert the deterministic fill-bar candle (decision bucket + M15)."""
    from app.data.ingest import seed_instruments
    from app.models.candle import CandleRow

    async with db_sessionmaker() as session:
        inst = (await seed_instruments(session, [SYMBOL]))[SYMBOL]
        session.add(
            CandleRow(
                instrument_id=inst.id,
                timeframe=TF,
                ts=_BUCKET + timedelta(minutes=15),
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
        await session.commit()


async def _count_ledger_rows(db_sessionmaker: Any) -> tuple[int, int]:
    """Return (orders, open positions) for the seeded symbol."""
    from app.models.paper_ledger import PaperOrderRow, PaperPositionRow, PaperPositionStatus

    async with db_sessionmaker() as session:
        orders = int(
            await session.scalar(
                select(func.count())
                .select_from(PaperOrderRow)
                .where(PaperOrderRow.symbol == SYMBOL)
            )
        )
        positions = int(
            await session.scalar(
                select(func.count())
                .select_from(PaperPositionRow)
                .where(
                    PaperPositionRow.symbol == SYMBOL,
                    PaperPositionRow.status == PaperPositionStatus.OPEN.value,
                )
            )
        )
    return orders, positions


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
# Phase 13C + 13D: PAPER decision -> PENDING order -> lifecycle fill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_decision_creates_order_and_position(
    db_sessionmaker: Any, fake_redis: Any
) -> None:
    """PAPER decision persists a PENDING order; the lifecycle fills it at the
    deterministic next-bar open (decision bucket + M15), producing the OPEN
    position. The order carries the sponsoring decision's id, proving decision
    -> ledger lineage in one atomic transaction.
    """
    from app.models.decision import DecisionRow
    from app.models.paper_ledger import PaperOrderRow, PaperPositionRow

    await _seed_decision_inputs(db_sessionmaker)
    await _config_pair(db_sessionmaker, actor="admin@example.com")
    worker = _orch_worker_with_broker(db_sessionmaker, fake_redis)
    await worker.ensure_groups()
    await _emit_signal_trigger(fake_redis, SYMBOL, TF)

    batch = await worker.poll_once()

    assert batch.errors == 0
    assert batch.processed >= 1
    assert await _count_decisions(db_sessionmaker) == 1

    # Trigger wiring submits PENDING only — no position until the lifecycle.
    orders, positions = await _count_ledger_rows(db_sessionmaker)
    assert orders == 1
    assert positions == 0

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
        order = (
            (await session.execute(select(PaperOrderRow).where(PaperOrderRow.symbol == SYMBOL)))
            .scalars()
            .first()
        )

    assert decision is not None and decision.status == "PAPER"
    assert order is not None
    assert order.side == "LONG"
    assert order.status == "PENDING"
    assert order.decision_id == decision.id  # 13C lineage

    # Close the fill bar and drive the lifecycle: the order fills at OPEN.
    await _add_fill_bar_candle(db_sessionmaker)
    result = await worker.process_lifecycle()
    assert result is not None
    assert result.fills == 1
    assert result.errors == 0

    orders, positions = await _count_ledger_rows(db_sessionmaker)
    assert orders == 1
    assert positions == 1

    async with db_sessionmaker() as session:
        position = (
            (
                await session.execute(
                    select(PaperPositionRow).where(PaperPositionRow.symbol == SYMBOL)
                )
            )
            .scalars()
            .first()
        )
    assert position is not None
    assert position.order_id == order.id
    assert position.side == "LONG"


@pytest.mark.asyncio
async def test_analysis_decision_never_creates_order(db_sessionmaker: Any, fake_redis: Any) -> None:
    """Low-agreement seed -> ANALYSIS; no paper order may ever appear."""
    await _seed_decision_inputs(
        db_sessionmaker,
        tech_conf="0.2",
        fund_direction="SHORT",
        fund_conf="0.9",
        senti_direction="SHORT",
        senti_conf="0.9",
    )
    await _config_pair(db_sessionmaker, actor="admin@example.com")
    worker = _orch_worker_with_broker(db_sessionmaker, fake_redis)
    await worker.ensure_groups()
    await _emit_signal_trigger(fake_redis, SYMBOL, TF)

    batch = await worker.poll_once()

    assert batch.errors == 0
    assert batch.processed >= 1
    assert batch.status_count.get("ANALYSIS", 0) >= 1
    assert await _count_decisions(db_sessionmaker) == 1
    assert await _count_ledger_rows(db_sessionmaker) == (0, 0)


@pytest.mark.asyncio
async def test_blocked_decision_never_creates_order(db_sessionmaker: Any, fake_redis: Any) -> None:
    """No candle -> no price -> risk fails closed; BLOCKED, never an order."""
    from app.models.decision import DecisionRow

    await _seed_decision_inputs(db_sessionmaker, with_candle=False)
    await _config_pair(db_sessionmaker, actor="admin@example.com")
    worker = _orch_worker_with_broker(db_sessionmaker, fake_redis)
    await worker.ensure_groups()
    await _emit_signal_trigger(fake_redis, SYMBOL, TF)

    batch = await worker.poll_once()

    assert batch.errors == 0
    assert batch.status_count.get("BLOCKED", 0) >= 1
    assert await _count_ledger_rows(db_sessionmaker) == (0, 0)

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
    assert decision is not None and decision.status == "BLOCKED"


@pytest.mark.asyncio
async def test_duplicate_trigger_creates_only_one_order(
    db_sessionmaker: Any, fake_redis: Any
) -> None:
    """Re-delivered trigger: second pass is a replay (created=False) -> 1 order."""
    await _seed_decision_inputs(db_sessionmaker)
    await _config_pair(db_sessionmaker, actor="admin@example.com")
    worker = _orch_worker_with_broker(db_sessionmaker, fake_redis)
    await worker.ensure_groups()

    await _emit_signal_trigger(fake_redis, SYMBOL, TF)
    first = await worker.poll_once()
    assert first.processed >= 1
    assert await _count_decisions(db_sessionmaker) == 1
    assert await _count_ledger_rows(db_sessionmaker) == (1, 0)  # PENDING, not yet filled

    await _add_fill_bar_candle(db_sessionmaker)
    result = await worker.process_lifecycle()
    assert result is not None and result.fills == 1
    assert await _count_ledger_rows(db_sessionmaker) == (1, 1)

    await _emit_signal_trigger(fake_redis, SYMBOL, TF)
    second = await worker.poll_once()
    assert second.errors == 0
    assert await _count_decisions(db_sessionmaker) == 1
    assert await _count_ledger_rows(db_sessionmaker) == (1, 1)  # no duplicate order


# ---------------------------------------------------------------------------
# Phase 14A: ledger-derived risk gates (daily-loss / exposure / drawdown)
# ---------------------------------------------------------------------------


async def _run_engine_gated(db_sessionmaker: Any) -> Any:
    """Restore the ledger, build the gate, and decide with it (14A worker path)."""
    from app.broker.ledger import LedgerBroker
    from app.broker.store import PostgresLedgerStore
    from app.core.config import get_settings
    from app.data.risk_config import load_risk_params
    from app.decisions.engine import DecisionEngine, OrchParams, RiskLive
    from app.decisions.ledger_gate import build_ledger_gate

    async with db_sessionmaker() as session:
        broker = LedgerBroker(store=PostgresLedgerStore(session=session))
        await broker.restore_state()
        view = await build_ledger_gate(broker=broker, symbol=SYMBOL, now=_NOW)

        engine = DecisionEngine(session=session, now=_NOW)
        risk = await load_risk_params(session, get_settings())
        result = await engine.decide(
            symbol=SYMBOL,
            timeframe=TF,
            configured=[TF],
            crafts=OrchParams(cooldown_seconds=0),
            risk=risk,
            gate=view.gate,
            live=RiskLive(
                equity=view.equity,
                peak_equity=view.peak_equity,
                cumulative_realized=view.cumulative_realized,
            ),
        )
        await session.commit()
        return result, view


async def _persist_closed_loss(db_sessionmaker: Any, *, loss_pct: float = 0.015) -> None:
    """Persist a realized CLOSED loss today (SL exit on a fresh paper account).

    ``loss_pct`` is relative to the 100k wallet; the position is sized so the
    loss lands within the current UTC day.
    """
    from app.agents.base import Direction
    from app.broker.ledger import LedgerBroker
    from app.broker.store import PostgresLedgerStore

    units = 100_000.0
    exit_price = 1.10 - (loss_pct * 100_000.0) / units

    async with db_sessionmaker() as session:
        broker = LedgerBroker(store=PostgresLedgerStore(session=session))
        order = await broker.submit_paper_order(
            symbol=SYMBOL,
            timeframe=TF,
            direction=Direction.LONG,
            ref_price=1.10,
            ts=_BUCKET,
            units=units,
            stop_loss=exit_price + 0.0001,
        )
        assert order is not None
        pos = await broker.fill_pending(order, open_price=1.10, ts=_BUCKET)
        assert pos is not None
        trade = await broker.evaluate_exit(symbol=SYMBOL, close=exit_price, ts=_NOW)
        assert trade is not None and trade.net_pnl < 0
        await broker.equity_snapshot(ts=_NOW)
        await session.commit()


async def _set_daily_loss_cap(db_sessionmaker: Any, *, cap: float) -> None:
    from app.data.risk_config import set_risk_params

    async with db_sessionmaker() as session:
        await set_risk_params(
            session, actor="admin@example.com", updates={"max_daily_loss_pct": cap}
        )
        await session.commit()


async def _risk_state_row(db_sessionmaker: Any, *, scope: str, key: str) -> Any:
    from app.models.risk_state import RiskStateRow

    async with db_sessionmaker() as session:
        return await session.scalar(
            select(RiskStateRow).where(RiskStateRow.scope == scope, RiskStateRow.period_key == key)
        )


@pytest.mark.asyncio
async def test_ledger_gate_persists_derived_risk_state(db_sessionmaker: Any) -> None:
    """A closed loss today drives the derived risk_state (D8 pass-through fix)."""
    await _seed_decision_inputs(db_sessionmaker)
    await _persist_closed_loss(db_sessionmaker, loss_pct=0.015)

    result, view = await _run_engine_gated(db_sessionmaker)

    assert result.status.value == "PAPER"  # 1.5% + risk margin < default 3% daily cap
    assert view.gate.daily_loss_used_pct > 0.0
    assert view.gate.drawdown_used_pct > 0.0
    assert view.gate.exposure_used_pct == 0.0  # the loss closed; no open exposure

    account = await _risk_state_row(db_sessionmaker, scope="account", key="global")
    daily = await _risk_state_row(
        db_sessionmaker, scope="daily", key=_NOW.astimezone(UTC).strftime("%Y-%m-%d")
    )
    assert account is not None and daily is not None
    assert float(daily.realized_loss) == pytest.approx(view.gate.daily_loss_used_pct, abs=1e-4)
    assert float(account.realized_loss) > 0.0
    assert float(account.peak_equity) == pytest.approx(view.peak_equity, abs=1e-4)
    assert float(account.max_drawdown) == pytest.approx(view.gate.drawdown_used_pct, abs=1e-4)
    assert float(account.exposure) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.asyncio
async def test_ledger_daily_loss_gate_blocks_next_paper_intent(db_sessionmaker: Any) -> None:
    """Realized loss above the cap vetoes the next PAPER intent (BLOCKED)."""
    from app.models.decision import DecisionRow, DecisionStatus

    await _seed_decision_inputs(db_sessionmaker)
    await _persist_closed_loss(db_sessionmaker, loss_pct=0.02)
    await _set_daily_loss_cap(db_sessionmaker, cap=0.015)  # 2% loss now over the cap

    result, _view = await _run_engine_gated(db_sessionmaker)

    assert result.status.value == "BLOCKED"
    assert result.veto_code == "daily_loss"

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
            .one()
        )
    assert decision.status == DecisionStatus.BLOCKED.value
    assert decision.veto_code == "daily_loss"


@pytest.mark.asyncio
async def test_pending_intent_does_not_consume_exposure(db_sessionmaker: Any) -> None:
    """An OPEN position is required to consume exposure — PENDING never does."""
    from app.agents.base import Direction
    from app.broker.ledger import LedgerBroker
    from app.broker.store import PostgresLedgerStore
    from app.data.risk_config import set_risk_params

    await _seed_decision_inputs(db_sessionmaker)
    async with db_sessionmaker() as session:
        await set_risk_params(
            session, actor="admin@example.com", updates={"max_exposure_pct": 0.05}
        )
        await session.commit()
    async with db_sessionmaker() as session:
        broker = LedgerBroker(store=PostgresLedgerStore(session=session))
        order = await broker.submit_paper_order(
            symbol=SYMBOL,
            timeframe=TF,
            direction=Direction.LONG,
            ref_price=1.10,
            ts=_BUCKET,
            units=100_000.0,
        )
        assert order is not None and order.status == "PENDING"
        await session.commit()

    result, view = await _run_engine_gated(db_sessionmaker)

    assert view.open_position_count == 0
    assert view.gate.exposure_used_pct == 0.0
    assert result.status.value == "PAPER"  # the PENDING intent consumed no capital


@pytest.mark.asyncio
async def test_open_position_exposure_gate_blocks_next_intent(db_sessionmaker: Any) -> None:
    """An OPEN position over the exposure cap vetoes the next PAPER intent."""
    from app.agents.base import Direction
    from app.broker.ledger import LedgerBroker
    from app.broker.store import PostgresLedgerStore
    from app.data.risk_config import set_risk_params

    await _seed_decision_inputs(db_sessionmaker)
    async with db_sessionmaker() as session:
        await set_risk_params(
            session, actor="admin@example.com", updates={"max_exposure_pct": 0.05}
        )
        await session.commit()
    async with db_sessionmaker() as session:
        broker = LedgerBroker(store=PostgresLedgerStore(session=session))
        order = await broker.submit_paper_order(
            symbol=SYMBOL,
            timeframe=TF,
            direction=Direction.LONG,
            ref_price=1.10,
            ts=_BUCKET,
            units=100_000.0,
        )
        assert order is not None
        pos = await broker.fill_pending(order, open_price=1.10, ts=_BUCKET)
        assert pos is not None  # notional 110k -> exposure 1.10 without the cap
        await broker.equity_snapshot(ts=_BUCKET)
        await session.commit()

    result, view = await _run_engine_gated(db_sessionmaker)

    assert view.open_position_count == 1
    assert view.gate.exposure_used_pct > 0.05
    assert result.status.value == "BLOCKED"
    assert result.veto_code == "exposure"


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
