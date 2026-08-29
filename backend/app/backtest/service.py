"""Backtest execution service: build inputs, run the driver, persist (Phase 6).

Uses the deterministic synthetic generator as the historical candle source
(decision D-D) so runs are reproducible from the same inputs. Replays persisted
fundamental/sentiment ``agent_signals`` for the range (decision D-B); missing
coverage is reported, never fabricated.

SAFE MODE: the service writes only to ``backtest_*`` tables. It never creates a
live order and never touches ``orders_paper``/``positions``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backtest.agent_runner import BacktestAgentRunner
from app.backtest.driver import BacktestDriver
from app.backtest.models import BacktestConfig
from app.backtest.repository import (
    create_run,
    mark_completed,
    mark_failed,
    save_equity_curve,
    save_trades,
)
from app.broker.costs import CostParams
from app.broker.paper import PaperBroker
from app.core.config import Settings
from app.data.providers.base import Candle
from app.data.providers.synthetic import generate_candle
from app.data.risk_config import RiskParams
from app.data.timeframes import align_to_bucket, iterate_buckets


def synthetic_candle_provider(
    symbol: str, timeframe: str, start: datetime, end: datetime
) -> list[Candle]:
    """Deterministic synthetic candles for [start, end)."""
    aligned_start = align_to_bucket(start, timeframe)
    return [
        generate_candle(symbol=symbol, timeframe=timeframe, bucket_start=bucket)
        for bucket in iterate_buckets(aligned_start, end, timeframe)
    ]


async def load_replayed_signals(
    session: AsyncSession,
    *,
    cfg: BacktestConfig,
) -> BacktestAgentRunner:
    """Load persisted fundamental/sentiment signals for the range into a runner."""
    from app.data.signal_repository import load_history, row_to_signal

    runner = BacktestAgentRunner()
    for symbol in cfg.symbols:
        for timeframe in cfg.timeframes:
            rows = await load_history(
                session,
                symbol=symbol,
                timeframe=timeframe,
                limit=100_000,
            )
            for row in rows:
                sig = row_to_signal(row)
                if (
                    sig.agent_id in ("fundamental", "sentiment")
                    and cfg.start <= sig.bucket_ts < cfg.end
                ):
                    runner.add_replayed(sig)
    return runner


async def run_backtest(
    *,
    cfg: BacktestConfig,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    """Execute and persist a backtest; returns the run id."""
    risk = RiskParams.from_settings(settings)
    async with session_factory() as session:
        run_id = await create_run(
            session,
            cfg=cfg,
            code_versions=_code_versions(),
        )
        await session.commit()

    try:
        cost_params = CostParams(spread=cfg.spread, slippage_pct=cfg.slippage_pct)
        async with session_factory() as session:
            runner = await load_replayed_signals(session, cfg=cfg)
        broker = PaperBroker(start_equity=cfg.start_equity, seed=cfg.seed, cost_params=cost_params)
        driver = BacktestDriver(
            cfg=cfg,
            risk=risk,
            broker=broker,
            candle_provider=synthetic_candle_provider,
            runner=runner,
        )
        result = driver.run()

        async with session_factory() as session:
            await save_trades(session, run_id=run_id, trades=result.trades)
            await save_equity_curve(session, run_id=run_id, points=result.equity_points)
            await mark_completed(session, run_id=run_id, metrics=result.metrics)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - persist run as FAILED
        async with session_factory() as session:
            await mark_failed(session, run_id=run_id, error=str(exc))
            await session.commit()
            raise
    return run_id


def _code_versions() -> dict[str, str]:
    return {
        "technical": "1",
        "regime": "1",
        "fundamental": "1",
        "sentiment": "1",
        "engine": "Phase5",
        "backtest": "Phase6",
    }


def config_from_dict(data: dict[str, Any]) -> BacktestConfig:
    """Build a validated BacktestConfig from API/CLI-provided fields (UTC)."""
    return BacktestConfig(
        symbols=tuple(sorted(s.upper() for s in data["symbols"])),
        timeframes=tuple(t.upper() for t in data["timeframes"]),
        start=_parse_dt(data["start"]),
        end=_parse_dt(data["end"]),
        seed=int(data.get("seed", 0)),
        start_equity=float(data.get("start_equity", 100_000.0)),
        warmup_bars=int(data.get("warmup_bars", 80)),
        spread=float(data.get("spread", 0.0001)),
        slippage_pct=float(data.get("slippage_pct", 0.00002)),
        require_full_coverage=bool(data.get("require_full_coverage", True)),
    )


def _parse_dt(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
