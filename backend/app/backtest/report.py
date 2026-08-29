"""Backtest reporting: aggregate metrics + equity/drawdown series (Phase 6).

Convention (decision D-D): Sharpe/Sortino are annualized using the *configured
timeframe* as the return period. The annualization factor is
``SECONDS_PER_YEAR / Timeframe.seconds(tf)`` (365.25-day year), so the basis is
derived from the selected timeframe rather than an unexplained hard-coded
number. Returns with zero standard deviation yield ``inf``-free 0.0.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from statistics import mean, pstdev

from app.backtest.models import BacktestConfig, RunMetrics
from app.broker.paper import Trade
from app.data.timeframes import Timeframe

SECONDS_PER_YEAR: float = 365.25 * 24 * 3600.0


def annualization_factor(timeframe: str) -> float:
    return SECONDS_PER_YEAR / float(Timeframe.seconds(timeframe))


def build_metrics(
    cfg: BacktestConfig,
    *,
    equity_points: Sequence[tuple[datetime, float]],
    trades: Sequence[Trade],
    broker_max_drawdown_pct: float,
    degraded_runs: int,
    coverage: list[dict[str, object]],
) -> RunMetrics:
    """Compute aggregate performance from equity points and closed trades."""
    if equity_points:
        _, start_equity = equity_points[0]
    else:
        start_equity = cfg.start_equity
    _, end_equity = equity_points[-1] if equity_points else (None, start_equity)
    net_pnl = end_equity - start_equity

    gross_pnl = 0.0
    total_costs = 0.0
    wins = [t for t in trades if t.net_pnl > 0]
    for t in trades:
        gross_pnl += t.gross_pnl
        total_costs += t.costs

    num_trades = len(trades)
    win_rate = (len(wins) / num_trades) if num_trades else 0.0
    positive = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    negative = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    profit_factor = (positive / negative) if negative else (float("inf") if positive > 0 else 0.0)

    sharpe, sortino = _risk_adjusted(equity_points, cfg.timeframes[0])

    exposure_avg = _avg_exposure(trades, start_equity)

    return RunMetrics(
        net_pnl=round(net_pnl, 6),
        gross_pnl=round(gross_pnl, 6),
        total_costs=round(total_costs, 6),
        num_trades=num_trades,
        win_rate=round(win_rate, 6),
        profit_factor=_finite(profit_factor),
        sharpe=_finite(sharpe),
        sortino=_finite(sortino),
        max_drawdown_pct=round(broker_max_drawdown_pct, 8),
        exposure_avg_pct=_finite(exposure_avg),
        bars=len(equity_points),
        degraded_runs=degraded_runs,
        coverage=coverage,
    )


def _risk_adjusted(
    equity_points: Sequence[tuple[datetime, float]], timeframe: str
) -> tuple[float, float]:
    if len(equity_points) < 2:
        return 0.0, 0.0
    prices = [p for _, p in equity_points]
    returns = [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))]
    if not returns:
        return 0.0, 0.0
    ann = annualization_factor(timeframe)
    mean_r = mean(returns)
    std = pstdev(returns)
    sharpe = (mean_r / std * (ann**0.5)) if std > 0 else 0.0
    downside_input = [r for r in returns if r < 0]
    downside = pstdev(downside_input) if downside_input else 0.0
    sortino = (mean_r / downside * (ann**0.5)) if downside > 0 else 0.0
    return sharpe, sortino


def _avg_exposure(trades: Sequence[Trade], start_equity: float) -> float:
    if not trades:
        return 0.0
    if start_equity <= 0:
        return 0.0
    total = sum(abs(t.entry_price * t.units) for t in trades)
    return total / len(trades) / start_equity


def _finite(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return value
