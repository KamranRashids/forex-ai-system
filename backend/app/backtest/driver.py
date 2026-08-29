"""Backtest replay driver (Phase 6).

Walks historical candles through the *same* decision path as production and
drives a deterministic ``PaperBroker``:

- timeframes are processed in rank order (large -> small); the smallest
  configured timeframe drives fills for a symbol, larger timeframes provide
  parent context only (a position is per-symbol, matching
  ``broker.positions.open_for``);
- decisions are generated from information available at the bar close; fills
  happen at the **next bar open** (no look-ahead);
- SL/TP are evaluated conservatively on bar closes (no favorable intrabar
  ordering, decision D-C);
- strict UTC throughout (timezone-independence lesson from the Phase 4 CI
  failure);
- missing historical fundamental/sentiment coverage is reported explicitly and
  never fabricated (decision D-B).

SAFE MODE: the driver writes only to in-memory broker state and (via the
caller) ``backtest_*`` persistence. It never creates a live order and never
touches ``orders_paper`` or ``positions``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.agents.base import Direction
from app.backtest.agent_runner import BacktestAgentRunner
from app.backtest.models import BacktestConfig, CoverageReport, RunMetrics
from app.backtest.orchestrator_sim import BacktestDecisionEngine
from app.backtest.report import build_metrics
from app.broker.paper import PaperBroker, Trade
from app.data.risk_config import RiskParams
from app.data.timeframes import Timeframe, align_to_bucket
from app.decisions.engine import DecisionAction
from app.decisions.risk import GateState

CandleProvider = Callable[[str, str, datetime, datetime], list[Any]]

#: Bounded trailing lookback for indicator windows. All technical/regime
#: indicators use fixed lookbacks well under this, so evaluating a bar on the
#: last ``_LOOKBACK`` candles is identical to the full history while keeping the
#: replay O(n) instead of O(n^2).
_LOOKBACK: int = 320


@dataclass(slots=True)
class DriverResult:
    run_id: str
    metrics: RunMetrics
    equity_points: list[tuple[datetime, float]] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    coverage: list[CoverageReport] = field(default_factory=list)
    decisions: int = 0
    paper_intents: int = 0


class BacktestDriver:
    def __init__(
        self,
        *,
        cfg: BacktestConfig,
        risk: RiskParams,
        broker: PaperBroker,
        candle_provider: CandleProvider,
        runner: BacktestAgentRunner | None = None,
    ) -> None:
        self.cfg = cfg
        self.risk = risk
        self.broker = broker
        self._candles = candle_provider
        self.engine = BacktestDecisionEngine(cfg)
        self._runner_impl: BacktestAgentRunner | None = runner
        self._fine = min(cfg.timeframes, key=Timeframe.rank)
        self._daily_loss: dict[str, float] = {}
        self._pending_fills: dict[str, _PendingFill] = {}
        self.equity_points: list[tuple[datetime, float]] = []
        self.trades: list[Trade] = []
        self.coverage: list[CoverageReport] = []
        self.decisions = 0
        self.paper_intents = 0
        self.degraded_runs = 0

    @property
    def runner(self) -> BacktestAgentRunner:
        if self._runner_impl is None:
            self._runner_impl = BacktestAgentRunner()
        return self._runner_impl

    # --- public API ------------------------------------------------------------

    def run(self) -> DriverResult:
        symbols = list(self.cfg.symbols)
        # Parent context first: process larger timeframes (excluding the fine
        # fill timeframe) so their decisions exist before the fine loop.
        for tf in sorted(self.cfg.timeframes, key=Timeframe.rank, reverse=True):
            if tf == self._fine:
                continue
            for symbol in symbols:
                self._run_context_tf(symbol, tf)

        for symbol in symbols:
            self._run_fine(symbol)

        self.trades = list(self.broker.trades)
        metrics = build_metrics(
            self.cfg,
            equity_points=self.equity_points,
            trades=self.trades,
            broker_max_drawdown_pct=self.broker.max_drawdown_pct,
            degraded_runs=self.degraded_runs,
            coverage=[c.to_dict() for c in self.coverage],
        )
        return DriverResult(
            run_id="",
            metrics=metrics,
            equity_points=self.equity_points,
            trades=self.trades,
            coverage=self.coverage,
            decisions=self.decisions,
            paper_intents=self.paper_intents,
        )

    # --- helpers ---------------------------------------------------------------

    def _run_context_tf(self, symbol: str, timeframe: str) -> None:
        """Compute + record decisions for a larger (context-only) timeframe."""
        bars = self._load_bars(symbol, timeframe)
        for i, bar in enumerate(bars):
            bucket = _bucket_of(bar)
            if i < self.cfg.warmup_bars:
                continue
            window = self._window(bars, i)
            prev_daily = self._prev_daily(symbol, bucket)
            run = self.runner.analyze(
                symbol=symbol,
                timeframe=timeframe,
                bucket_ts=bucket,
                candles=window,
                now=bucket,
                prev_daily=prev_daily,
                pip_size=self._pip_size(symbol),
            )
            signals = run.signals
            if not signals or "technical" not in signals:
                continue
            price = _close_of(window[-1])
            outcome = self.engine.decide(
                symbol=symbol,
                timeframe=timeframe,
                bucket_ts=bucket,
                now=bucket,
                signals=signals,
                price=price,
                configured=list(self.cfg.timeframes),
                gate=GateState(),
                risk=self.risk,
            )
            if outcome.action == DecisionAction.PERSIST:
                self.decisions += 1
            # context frames never open positions; broker marks only at close.

    def _run_fine(self, symbol: str) -> None:
        bars = self._load_bars(symbol, self._fine)
        if not bars:
            return
        counts = {"technical": 0, "regime": 0, "fundamental": 0, "sentiment": 0}
        last_price: float | None = None

        for i, bar in enumerate(bars):
            bucket = _bucket_of(bar)
            open_px = _open_of(bar)
            close_px = _close_of(bar)
            last_price = close_px

            # 1. Execute any pending fill from the previous bar's decision at
            #    this bar's OPEN (no look-ahead: the decision used <= prev close).
            pending_act = self._pending_fills.pop(symbol, None)
            if pending_act is not None:
                self._apply_fill(symbol, pending_act, open_px, bucket)

            # 2. Evaluate SL/TP conservatively at this bar's CLOSE.
            self.broker.evaluate_exit(symbol=symbol, close=close_px, ts=bucket)

            # 3. Generate decision at close (only after warmup).
            if i >= self.cfg.warmup_bars:
                window = self._window(bars, i)
                prev_daily = self._prev_daily(symbol, bucket)
                run = self.runner.analyze(
                    symbol=symbol,
                    timeframe=self._fine,
                    bucket_ts=bucket,
                    candles=window,
                    now=bucket,
                    prev_daily=prev_daily,
                    pip_size=self._pip_size(symbol),
                )
                self._tally(run, counts)
                signals = run.signals
                if signals and "technical" in signals:
                    outcome = self.engine.decide(
                        symbol=symbol,
                        timeframe=self._fine,
                        bucket_ts=bucket,
                        now=bucket,
                        signals=signals,
                        price=close_px,
                        configured=list(self.cfg.timeframes),
                        gate=self._gate(symbol, bucket),
                        risk=self.risk,
                    )
                    if outcome.action == DecisionAction.PERSIST:
                        self.decisions += 1
                        if outcome.status and outcome.status.value == "PAPER":
                            self.paper_intents += 1
                            self._schedule_fill(symbol, len(bars), i, outcome)
                        elif outcome.status and outcome.status.value == "BLOCKED":
                            # Risk refuses new entries; keep current position.
                            pass

            # 4. Mark equity at this bar's close (per-symbol mark).
            self.broker.mark_position(symbol, last_price)
            equity = self.broker.state(bucket).equity
            self._record_equity(bucket, equity)

        self._append_coverage(symbol, self._fine, bars, counts)
        # Any still-open position is left unrealized (report uses closed pnl).

    def _schedule_fill(
        self,
        symbol: str,
        total_bars: int,
        i: int,
        outcome: Any,
    ) -> None:
        if i + 1 >= total_bars:
            return
        sizing = outcome.risk.sizing if outcome.risk else None
        units = sizing.position_size_units if sizing else 0.0
        current = self.broker.positions.open_for(symbol)
        direction = outcome.fused.direction
        if direction == Direction.FLAT:
            return
        if current is not None and current.side == direction:
            return  # already long/short as signalled; keep
        close_existing = current is not None and current.side != direction
        self._pending_fills[symbol] = _PendingFill(
            close_existing=close_existing,
            open_direction=direction,
            units=units,
            stop_loss=sizing.stop_loss if sizing else None,
            take_profit=sizing.take_profit if sizing else None,
        )

    def _apply_fill(self, symbol: str, act: _PendingFill, price: float, ts: datetime) -> None:
        if act.close_existing:
            self.broker.close_on_signal(symbol=symbol, price=price, ts=ts)
        self.broker.enter_at_next_open(
            symbol=symbol,
            timeframe=self._fine,
            direction=act.open_direction,
            ref_price=price,
            ts=ts,
            stop_loss=act.stop_loss,
            take_profit=act.take_profit,
            units=act.units,
        )

    # --- state / risk -----------------------------------------------------------

    def _gate(self, symbol: str, ts: datetime) -> GateState:
        positions = self.broker.positions
        equity = self.broker.total_equity() or self.cfg.start_equity
        exposure = positions.total_notional() / equity
        basket = positions.basket_notional(symbol)
        correlation_pct = basket / equity
        trigger = positions.correlation_triggered(symbol)
        # Daily-loss accumulation is keyed by the *bar's* UTC date so results are
        # host-timezone independent (strict UTC, decision D-D / P4 lesson).
        self._sync_daily_loss()
        daily_key = ts.astimezone(UTC).date().isoformat()
        daily_loss = self._daily_loss.get(daily_key, 0.0)
        daily_loss_pct = (
            max(0.0, -daily_loss) / self.cfg.start_equity if self.cfg.start_equity else 0.0
        )
        return GateState(
            exposure_used_pct=exposure,
            correlation_used_pct=correlation_pct,
            daily_loss_used_pct=daily_loss_pct,
            drawdown_used_pct=self.broker.max_drawdown_pct,
            correlation_triggered=trigger,
        )

    def _sync_daily_loss(self) -> None:
        """Rebuild realized-loss-by-UTC-date from the broker's closed trades."""
        self._daily_loss = {}
        for t in self.broker.trades:
            date_key = t.exit_ts.astimezone(UTC).date().isoformat()
            self._daily_loss[date_key] = self._daily_loss.get(date_key, 0.0) + t.net_pnl

    def _record_equity(self, ts: datetime, equity: float) -> None:
        if self.equity_points and self.equity_points[-1][0] == ts:
            self.equity_points[-1] = (ts, equity)
        else:
            self.equity_points.append((ts, equity))

    # --- data loading ------------------------------------------------------------

    def _load_bars(self, symbol: str, timeframe: str) -> list[Any]:
        start = align_to_bucket(self.cfg.start, timeframe)
        end = self.cfg.end
        return self._candles(symbol, timeframe, start, end)

    def _window(self, bars: list[Any], i: int) -> list[Any]:
        start = max(0, i + 1 - _LOOKBACK)
        return bars[start : i + 1]

    def _prev_daily(self, symbol: str, bucket: datetime) -> dict[str, float] | None:
        if "D1" not in self.cfg.timeframes:
            return None
        d1_bars = self._load_bars(symbol, "D1")
        prev = None
        for bar in d1_bars:
            if _bucket_of(bar) + timedelta(days=1) <= bucket:
                prev = bar
            else:
                break
        if prev is None:
            return None
        return {
            "high": float(_high_of(prev)),
            "low": float(_low_of(prev)),
            "close": float(_close_of(prev)),
        }

    def _pip_size(self, symbol: str) -> float:
        return 0.01 if symbol.endswith("JPY") else 0.0001

    def _tally(self, run: Any, counts: dict[str, int]) -> None:
        for agent in ("technical", "regime", "fundamental", "sentiment"):
            if agent in run.signals:
                counts[agent] += 1

    def _append_coverage(
        self, symbol: str, timeframe: str, bars: list[Any], counts: dict[str, int]
    ) -> None:
        expected = len(bars) - self.cfg.warmup_bars
        deg = counts["fundamental"] < expected or counts["sentiment"] < expected
        if deg:
            self.degraded_runs += 1
        self.coverage.append(
            CoverageReport(
                symbol=symbol,
                timeframe=timeframe,
                expected_bars=max(0, expected),
                technical=counts["technical"],
                regime=counts["regime"],
                fundamental=counts["fundamental"],
                sentiment=counts["sentiment"],
                degraded=deg,
            )
        )


@dataclass(frozen=True, slots=True)
class _PendingFill:
    close_existing: bool
    open_direction: Direction
    units: float
    stop_loss: float | None
    take_profit: float | None


def _bucket_of(bar: Any) -> datetime:
    raw: Any = bar.bucket_start if hasattr(bar, "bucket_start") else bar.ts
    ts: datetime = raw if isinstance(raw, datetime) else raw.replace(tzinfo=UTC)
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _open_of(bar: Any) -> float:
    return float(bar.open)


def _close_of(bar: Any) -> float:
    return float(bar.close)


def _high_of(bar: Any) -> float:
    return float(bar.high)


def _low_of(bar: Any) -> float:
    return float(bar.low)
