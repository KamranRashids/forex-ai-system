"""Backtest configuration, inputs, and reproducible identifiers (Phase 6)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """White-box reproduction inputs for one backtest run (frozen/serializable)."""

    start: datetime
    end: datetime
    symbols: tuple[str, ...] = ("EURUSD", "GBPUSD")
    timeframes: tuple[str, ...] = ("H1",)
    seed: int = 0
    start_equity: float = 100_000.0
    #: Warmup bars ignored before decisions can be produced per timeframe.
    warmup_bars: int = 60
    spread: float = 0.0001
    slippage_pct: float = 0.00002
    #: If True, missing historical fundamental/sentiment signals degrade
    #: coverage and are reported explicitly rather than fabricated.
    require_full_coverage: bool = True

    def to_jsonable(self) -> dict[str, object]:
        data = asdict(self)
        data["start"] = self.start.isoformat()
        data["end"] = self.end.isoformat()
        data["symbols"] = list(self.symbols)
        data["timeframes"] = list(self.timeframes)
        return data


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Explicit account of replay coverage for one symbol/timeframe."""

    symbol: str
    timeframe: str
    expected_bars: int
    technical: int
    regime: int
    fundamental: int
    sentiment: int
    degraded: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "expected_bars": self.expected_bars,
            "technical": self.technical,
            "regime": self.regime,
            "fundamental": self.fundamental,
            "sentiment": self.sentiment,
            "degraded": self.degraded,
        }


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """Aggregate backtest performance metrics (JSON-serializable)."""

    net_pnl: float = 0.0
    gross_pnl: float = 0.0
    total_costs: float = 0.0
    num_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown_pct: float = 0.0
    exposure_avg_pct: float = 0.0
    bars: int = 0
    degraded_runs: int = 0
    coverage: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
