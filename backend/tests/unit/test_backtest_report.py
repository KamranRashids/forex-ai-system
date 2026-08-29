"""Unit tests: backtest metrics/reporting (annualization, aggregation)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agents.base import Direction
from app.backtest.models import BacktestConfig, RunMetrics
from app.backtest.report import annualization_factor, build_metrics
from app.broker.paper import Trade

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _cfg() -> BacktestConfig:
    return BacktestConfig(start=_T0, end=_T0 + timedelta(days=10), timeframes=("H1",))


def _trade(
    ts: datetime,
    net: float,
    gross: float = 0.0,
    costs: float = 0.0,
    price: float = 1.1,
    units: float = 1000.0,
) -> Trade:
    return Trade(
        symbol="EURUSD",
        timeframe="H1",
        side=Direction.LONG,
        units=units,
        entry_ts=ts,
        entry_price=price,
        exit_ts=ts + timedelta(hours=1),
        exit_price=price + gross / units,
        gross_pnl=gross,
        costs=costs,
        net_pnl=net,
        exit_reason="signal",
    )


def test_annualization_factor_per_timeframe():
    h1 = annualization_factor("H1")
    d1 = annualization_factor("D1")
    assert 0 < d1 < h1  # daily basis is finer -> smaller factor


def test_build_metrics_flat_no_trades():
    cfg = _cfg()
    points = [
        (cfg.start, 100_000.0),
        (cfg.start + timedelta(hours=1), 99_000.0),
    ]
    m = build_metrics(
        cfg,
        equity_points=points,
        trades=[],
        broker_max_drawdown_pct=0.01,
        degraded_runs=0,
        coverage=[],
    )
    assert isinstance(m, RunMetrics)
    assert m.net_pnl == -1000.0
    assert m.num_trades == 0
    assert m.win_rate == 0.0
    assert m.bars == 2


def test_build_metrics_win_rate_and_aggregates():
    cfg = _cfg()
    trades = [
        _trade(cfg.start, net=100.0, gross=105.0, costs=5.0),
        _trade(cfg.start + timedelta(hours=1), net=-40.0, gross=-35.0, costs=5.0),
        _trade(cfg.start + timedelta(hours=2), net=60.0, gross=64.0, costs=4.0),
    ]
    m = build_metrics(
        cfg,
        equity_points=[(cfg.start, 100_000.0)],
        trades=trades,
        broker_max_drawdown_pct=0.0,
        degraded_runs=0,
        coverage=[],
    )
    assert m.num_trades == 3
    assert round(m.win_rate, 6) == round(2 / 3, 6)
    assert m.gross_pnl == 105.0 - 35.0 + 64.0
    assert m.total_costs == 14.0


def test_infinite_profit_factor_is_not_serialized_as_inf():
    cfg = _cfg()
    trades = [_trade(cfg.start, net=100.0)]  # no losing trade -> inf pf
    m = build_metrics(
        cfg,
        equity_points=[(cfg.start, 100_000.0)],
        trades=trades,
        broker_max_drawdown_pct=0.0,
        degraded_runs=0,
        coverage=[{"c": 1}],
    )
    d = m.to_dict()
    assert d["profit_factor"] != float("inf")
    assert d["profit_factor"] == 0.0 or d["profit_factor"] > 0.0


def test_sortino_without_downside_is_zero_not_error():
    cfg = _cfg()
    points = [
        (cfg.start, 100_000.0),
        (cfg.start + timedelta(hours=1), 101_000.0),
        (cfg.start + timedelta(hours=2), 102_000.0),
    ]
    m = build_metrics(
        cfg,
        equity_points=points,
        trades=[],
        broker_max_drawdown_pct=0.0,
        degraded_runs=0,
        coverage=[],
    )
    assert m.sortino == 0.0
