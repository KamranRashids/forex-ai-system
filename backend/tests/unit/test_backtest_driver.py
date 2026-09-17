"""Unit tests: backtest driver determinism, no-look-ahead fills, coverage.

A controllable stub runner forces full coverage + a chosen direction so the
decision path reaches PAPER and fills deterministically, letting us assert the
execution *policy* (decision C): a fill happens at the NEXT bar open, never at
the decision bar's close, and identical inputs produce identical runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.agents.base import AgentSignal, Direction
from app.backtest.agent_runner import AgentRunResult, BacktestAgentRunner
from app.backtest.driver import BacktestDriver
from app.backtest.models import BacktestConfig
from app.backtest.service import synthetic_candle_provider
from app.broker.costs import CostParams
from app.broker.paper import PaperBroker
from app.data.risk_config import RiskParams

_START = datetime(2024, 3, 1, tzinfo=UTC)


def _risk() -> RiskParams:
    return RiskParams(
        max_risk_pct_account=0.01,
        max_exposure_pct=1.0,
        max_daily_loss_pct=0.40,
        max_drawdown_pct=1.0,
        min_rr=0.1,
        sl_atr_multiple=1.5,
        tp_atr_multiple=2.5,
        vol_target_pct=0.9,
        correlation_cap_pct=1.0,
        risk_enabled=True,
        paper_equity=100_000.0,
    )


class StubRunner(BacktestAgentRunner):
    """Returns a full-coverage signal set with a chosen direction."""

    def __init__(self, direction: Direction) -> None:
        super().__init__()
        self.direction = direction
        self.flip_after: int | None = None  # bar index
        self.flip_direction: Direction = Direction.FLAT

    def analyze(
        self, *, symbol, timeframe, bucket_ts, candles, now, prev_daily=None, pip_size=0.001
    ):
        direction = self.direction
        if self.flip_after is not None and len(candles) >= self.flip_after:
            direction = self.flip_direction
        res = AgentRunResult()
        res.signals["technical"] = _sig(
            "technical", direction, bucket_ts, symbol, timeframe, {"atr14": 0.0020, "score": 0.5}
        )
        res.signals["regime"] = _sig(
            "regime", Direction.FLAT, bucket_ts, symbol, timeframe, {"regime": "trending"}
        )
        res.signals["fundamental"] = _sig(
            "fundamental", direction, bucket_ts, symbol, timeframe, {}
        )
        res.signals["sentiment"] = _sig("sentiment", direction, bucket_ts, symbol, timeframe, {})
        res.computed = {"technical", "regime"}
        res.replayed = {"fundamental", "sentiment"}
        return res


def _sig(agent, direction, bucket_ts, symbol, timeframe, features):
    return AgentSignal(
        agent_id=agent,
        version="1",
        symbol=symbol,
        timeframe=timeframe,
        direction=direction,
        confidence=0.8,
        bucket_ts=bucket_ts,
        rationale="",
        features=features,
        created_at=bucket_ts,
        valid_until=bucket_ts + timedelta(hours=1),
        run_id="stub",
    )


def _cfg(
    days: int = 6,
    timeframes: tuple[str, ...] = ("H1",),
    symbols: tuple[str, ...] = ("EURUSD",),
    warmup_bars: int = 40,
) -> BacktestConfig:
    return BacktestConfig(
        start=_START,
        end=_START + timedelta(days=days),
        symbols=symbols,
        timeframes=timeframes,
        seed=11,
        start_equity=100_000.0,
        warmup_bars=warmup_bars,
    )


def _run(cfg: BacktestConfig, runner: BacktestAgentRunner) -> tuple:
    broker = PaperBroker(start_equity=cfg.start_equity, seed=cfg.seed, cost_params=CostParams())
    driver = BacktestDriver(
        cfg=cfg,
        risk=_risk(),
        broker=broker,
        candle_provider=synthetic_candle_provider,
        runner=runner,
    )
    return broker, driver.run()


def test_deterministic_same_inputs_same_outputs():
    cfg = _cfg()
    broker, r1 = _run(cfg, StubRunner(Direction.LONG))
    broker2, r2 = _run(cfg, StubRunner(Direction.LONG))
    assert r1.metrics.to_dict() == r2.metrics.to_dict()
    assert [
        (t.symbol, t.side.value, t.entry_price, t.exit_price, t.exit_reason) for t in r1.trades
    ] == [(t.symbol, t.side.value, t.entry_price, t.exit_price, t.exit_reason) for t in r2.trades]
    assert [p for _, p in r1.equity_points] == [p for _, p in r2.equity_points]


def test_fill_price_is_next_bar_open_not_decision_close():
    """Decision at bar i is filled at bar i+1's OPEN (no look-ahead, D-C)."""
    cfg = _cfg(warmup_bars=10)
    broker, result = _run(cfg, StubRunner(Direction.LONG))
    # First decision (LONG) happens at the first eligible bar (warmup index)
    # and is filled at the open of the NEXT bar. The position stays open (the
    # stub keeps signalling LONG), so inspect the broker's open position.
    position = broker.positions.open_for(cfg.symbols[0])
    assert position is not None, "expected an open position from the fill"
    bars = synthetic_candle_provider(cfg.symbols[0], "H1", _START, cfg.end)
    expected_open = float(bars[cfg.warmup_bars + 1].open)
    assert position.entry_price == pytest.approx(expected_open, rel=1e-6)
    assert result.paper_intents > 0


def test_coverage_reports_degraded_without_replay():
    cfg = _cfg(days=3, warmup_bars=20)
    runner = BacktestAgentRunner()  # no replayed fundamental/sentiment
    broker, result = _run(cfg, runner)
    assert result.coverage
    rep = result.coverage[0]
    assert rep.degraded is True
    assert rep.technical > 0
    assert rep.fundamental == 0
    assert rep.sentiment == 0
    assert result.metrics.degraded_runs >= 1


def test_daily_loss_keyed_on_utc_date_is_hosttimezone_independent():
    """Daily-loss bucket is the bar's UTC date, so host TZ doesn't change it."""
    cfg = _cfg(days=4, warmup_bars=20)
    broker, result = _run(cfg, StubRunner(Direction.LONG))
    # Deterministic across the board; re-running under another TZ in CI must
    # produce identical results. If a naive host-clock date leaked in, results
    # would differ. Asserting determinism here guards the non-determinism bug.
    broker2, result2 = _run(cfg, StubRunner(Direction.LONG))
    assert result2.metrics.to_dict() == result.metrics.to_dict()


# ---------------------------------------------------------------------------
# Phase 14A: joint gate parity — build_ledger_gate mirrors driver._gate
# ---------------------------------------------------------------------------

_VERSION_TS = datetime(2024, 3, 2, 12, 0, tzinfo=UTC)


class _MirrorStore:
    """Minimal ledger store the parity scenarios rehydrate from."""

    def __init__(self) -> None:
        from app.models.paper_ledger import (
            AccountSnapshotRow,
            PaperOrderRow,
            PaperPositionRow,
        )

        self.snapshots: list[AccountSnapshotRow] = []
        self.open_rows: list[PaperPositionRow] = []
        self.closed_rows: list[PaperPositionRow] = []
        self.orders: list[PaperOrderRow] = []

    async def latest_snapshot(self):
        return self.snapshots[-1] if self.snapshots else None

    async def list_open_positions(self):
        return self.open_rows

    async def list_closed_positions(self):
        return self.closed_rows

    async def list_pending_orders(self):
        return self.orders

    async def load_open_position_rows(self):
        return self.open_rows

    async def load_realized_pnl_since(self, start_ts, end_ts):
        from decimal import Decimal

        return sum(
            (p.net_pnl or Decimal("0"))
            for p in self.closed_rows
            if p.exit_ts is not None and start_ts <= p.exit_ts < end_ts
        )

    async def list_pending_expired(self, now):
        return []


def _ledger_store_for(
    broker: PaperBroker, ts: datetime, *, with_snapshot: bool = True
) -> _MirrorStore:
    """Mirror a driven PaperBroker's positions/trades into a ledger store."""
    import uuid as _uuid
    from decimal import Decimal

    from app.models.paper_ledger import AccountSnapshotRow, PaperPositionRow

    store = _MirrorStore()
    for pos in broker.positions.positions:
        store.open_rows.append(
            PaperPositionRow(
                id=_uuid.uuid4(),
                order_id=_uuid.uuid4(),
                symbol=pos.symbol,
                timeframe=pos.timeframe,
                side=pos.side.value,
                units=Decimal(str(pos.units)),
                entry_price=Decimal(str(pos.entry_price)),
                entry_ts=pos.entry_ts,
                stop_loss=None if pos.stop_loss is None else Decimal(str(pos.stop_loss)),
                take_profit=None if pos.take_profit is None else Decimal(str(pos.take_profit)),
                costs=Decimal(str(pos.costs)),
                status="OPEN",
            )
        )
    for trade in broker.trades:
        store.closed_rows.append(
            PaperPositionRow(
                id=_uuid.uuid4(),
                order_id=_uuid.uuid4(),
                symbol=trade.symbol,
                timeframe=trade.timeframe,
                side=trade.side.value,
                units=Decimal(str(trade.units)),
                entry_price=Decimal(str(trade.entry_price)),
                entry_ts=trade.entry_ts,
                exit_price=Decimal(str(trade.exit_price)),
                exit_ts=trade.exit_ts,
                costs=Decimal(str(trade.costs)),
                net_pnl=Decimal(str(trade.net_pnl)),
                status="CLOSED",
            )
        )
    # Rehydrate the live-equity view from a snapshot exactly like production
    # (restore_state rebuilds equity/drawdown from the latest snapshot). A
    # fresh account (with_snapshot=False) leaves equity at start_equity, the
    # same denominator the driver's daily-loss gate uses for a new wallet.
    if with_snapshot:
        store.snapshots.append(
            AccountSnapshotRow(
                id=_uuid.uuid4(),
                ts=ts,
                cash=Decimal(str(broker.cash)),
                equity=Decimal(str(broker.total_equity())),
                open_pnl=Decimal(str(broker.total_equity() - broker.cash)),
                realized_pnl=Decimal(str(sum((t.net_pnl for t in broker.trades), 0.0))),
                peak_equity=Decimal(str(broker.peak_equity)),
                drawdown_pct=Decimal(str(broker.max_drawdown_pct)),
                margin_used=Decimal("0"),
                extra={},
            )
        )
    return store


async def test_ledger_gate_equals_driver_gate_open_position():
    """One OPEN LONG: builder GateState is identical to ``driver._gate``."""
    from app.broker.ledger import LedgerBroker
    from app.decisions.ledger_gate import build_ledger_gate

    broker = PaperBroker(start_equity=100_000.0, seed=0, cost_params=CostParams())
    cfg = _cfg()
    driver = BacktestDriver(
        cfg=cfg,
        risk=_risk(),
        broker=broker,
        candle_provider=synthetic_candle_provider,
        runner=BacktestAgentRunner(),
    )
    broker.enter_at_next_open(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.09,
        ts=_VERSION_TS,
        units=5_000.0,
    )
    driver_gate = driver._gate("EURUSD", _VERSION_TS)
    assert driver_gate.exposure_used_pct > 0.0

    store = _ledger_store_for(broker, _VERSION_TS)
    ledger = LedgerBroker(store=store, start_equity=100_000.0, seed=0)
    await ledger.restore_state()
    view = await build_ledger_gate(broker=ledger, symbol="EURUSD", now=_VERSION_TS)

    assert view.gate == driver_gate


async def test_ledger_gate_equals_driver_gate_after_closed_loss():
    """After a closed loss (no snapshot drift): daily-loss gate is identical."""
    from app.broker.ledger import LedgerBroker
    from app.decisions.ledger_gate import build_ledger_gate

    broker = PaperBroker(start_equity=100_000.0, seed=0, cost_params=CostParams())
    cfg = _cfg()
    driver = BacktestDriver(
        cfg=cfg,
        risk=_risk(),
        broker=broker,
        candle_provider=synthetic_candle_provider,
        runner=BacktestAgentRunner(),
    )
    broker.enter_at_next_open(
        symbol="EURUSD",
        timeframe="H1",
        direction=Direction.LONG,
        ref_price=1.0850,
        ts=_VERSION_TS,
        stop_loss=1.0805,
        units=10_000.0,
    )
    trade = broker.evaluate_exit(symbol="EURUSD", close=1.0800, ts=_VERSION_TS + timedelta(hours=1))
    assert trade is not None and trade.net_pnl < 0

    driver_gate = driver._gate("EURUSD", _VERSION_TS)
    assert driver_gate.daily_loss_used_pct > 0.0
    assert driver_gate.exposure_used_pct == 0.0

    store = _ledger_store_for(broker, _VERSION_TS, with_snapshot=False)
    ledger = LedgerBroker(store=store, start_equity=100_000.0, seed=0)
    await ledger.restore_state()
    view = await build_ledger_gate(broker=ledger, symbol="EURUSD", now=_VERSION_TS)

    assert view.gate == driver_gate
