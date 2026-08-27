"""Risk agent: fail-closed gating and paper sizing (Phase 5, approved §4.4).

The risk agent is the **final gate** before a paper analysis intent can be
promoted. It never creates an order; it computes a *paper* position size and
stop/take-profit, then independently evaluates four gates:

1. **Exposure**  — total open paper notional must stay under the cap.
2. **Correlation** — notional on correlated baskets must stay under the cap.
3. **Daily-loss** — realized paper loss today must stay under the cap.
4. **Drawdown** — account drawdown must stay under the cap.

Fail-closed policy: if any gate cannot be evaluated (missing ATR, missing
state), that gate is treated as **failed**. A decision can only be promoted to
PAPER when every gate returns ``ok=True``; otherwise it is BLOCKED. Analysis is
always recorded, but pausing/promoting never bypasses a failing gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agents.base import Direction
from app.data.risk_config import RiskParams
from app.decisions.fusion import sign_of

_GATE_KEYS = ("rr", "exposure", "correlation", "daily_loss", "drawdown")


@dataclass(frozen=True, slots=True)
class SizingResult:
    position_size_units: float
    stop_loss: float
    take_profit: float
    rr_ratio: float
    risk_pct_account: float
    atr: float
    price: float


@dataclass(frozen=True, slots=True)
class GateState:
    """Runtime aggregate state fed into the gates (gathered by orchestrator)."""

    exposure_used_pct: float = 0.0
    correlation_used_pct: float = 0.0
    daily_loss_used_pct: float = 0.0
    drawdown_used_pct: float = 0.0
    #: True when the candidate position would enter a correlated basket.
    correlation_triggered: bool = False


@dataclass(frozen=True, slots=True)
class GateReason:
    code: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class RiskOutcome:
    passed: bool
    sizing: SizingResult | None
    gates: dict[str, GateReason]
    paper: bool

    @property
    def reasons(self) -> list[dict[str, Any]]:
        return [{"code": r.code, "ok": r.ok, "detail": r.detail} for r in self.gates.values()]


@dataclass(frozen=True, slots=True)
class RiskDeps:
    """Optional reasoning dependencies; kept minimal and safe for Phase 5.

    A ``atr`` of None means SL/TP cannot be sized; the agent then fails closed
    for promotion (paper=False) but still records the analysis.
    """

    atr: float | None = None
    price: float | None = None


def compute_sizing(
    *,
    price: float,
    atr: float,
    direction: Direction,
    params: RiskParams,
) -> SizingResult:
    """Pure paper-sizing math from ATR-driven SL/TP targets."""
    s = sign_of(direction)
    sl_distance = params.sl_atr_multiple * atr
    tp_distance = params.tp_atr_multiple * atr
    stop_loss = round(price - s * sl_distance, 8)
    take_profit = round(price + s * tp_distance, 8)
    rr = round(tp_distance / sl_distance, 4) if sl_distance > 0 else 0.0

    risk_amount = params.paper_equity * params.max_risk_pct_account
    units_by_risk = risk_amount / sl_distance if sl_distance > 0 else 0.0
    # Exposure cap: notional must not exceed equity * max_exposure_pct.
    notional_cap = params.paper_equity * params.max_exposure_pct
    units_by_exposure = notional_cap / price if price > 0 else 0.0
    units = round(min(units_by_risk, units_by_exposure), 6)

    notional = units * price
    if params.paper_equity:
        risk_pct = round(notional * sl_distance / params.paper_equity, 4)
    else:
        risk_pct = 0.0
    return SizingResult(
        position_size_units=units,
        stop_loss=stop_loss,
        take_profit=take_profit,
        rr_ratio=rr,
        risk_pct_account=min(risk_pct, 1.0),
        atr=atr,
        price=price,
    )


def assess(
    *,
    direction: Direction,
    deps: RiskDeps,
    gate: GateState,
    params: RiskParams,
) -> RiskOutcome:
    """Evaluate the full risk pass for one candidate decision.

    Returns a ``RiskOutcome`` with ``passed`` True only iff *every* gate is
    ok AND the sizing is possible AND the direction is actionable (not FLAT).
    """
    gates: dict[str, GateReason] = {}

    if direction == Direction.FLAT:
        for key in _GATE_KEYS:
            gates[key] = GateReason(key, True, "flat direction requires no sizing")
        gates["flat"] = GateReason("flat", True, "flat direction requires no sizing")
        return RiskOutcome(passed=True, sizing=None, gates=gates, paper=False)

    # Fail closed when we cannot size.
    if deps.atr is None or deps.atr <= 0 or deps.price is None or deps.price <= 0:
        for key in _GATE_KEYS:
            gates[key] = GateReason(key, False, "cannot size: ATR/price unavailable")
        gates["sizing"] = GateReason(
            "sizing_impossible", False, "ATR/price unavailable; cannot size"
        )
        return RiskOutcome(passed=False, sizing=None, gates=gates, paper=False)

    sizing = compute_sizing(price=deps.price, atr=deps.atr, direction=direction, params=params)

    gates["rr"] = GateReason(
        "rr", sizing.rr_ratio >= params.min_rr, f"rr={sizing.rr_ratio:.3f} (min {params.min_rr})"
    )
    gates["exposure"] = GateReason(
        "exposure",
        gate.exposure_used_pct + params.max_risk_pct_account <= params.max_exposure_pct,
        f"exposure={gate.exposure_used_pct:.4f} (cap {params.max_exposure_pct})",
    )
    gates["correlation"] = GateReason(
        "correlation",
        not gate.correlation_triggered
        or gate.correlation_used_pct + params.max_risk_pct_account <= params.correlation_cap_pct,
        f"corr_basket={gate.correlation_used_pct:.4f} (cap {params.correlation_cap_pct})",
    )
    gates["daily_loss"] = GateReason(
        "daily_loss",
        gate.daily_loss_used_pct + params.max_risk_pct_account <= params.max_daily_loss_pct,
        f"daily_loss={gate.daily_loss_used_pct:.4f} (cap {params.max_daily_loss_pct})",
    )
    gates["drawdown"] = GateReason(
        "drawdown",
        gate.drawdown_used_pct + params.max_risk_pct_account <= params.max_drawdown_pct,
        f"drawdown={gate.drawdown_used_pct:.4f} (cap {params.max_drawdown_pct})",
    )

    all_ok = all(g.ok for g in gates.values())
    paper = all_ok and params.risk_enabled
    return RiskOutcome(
        passed=all_ok,
        sizing=sizing,
        gates=gates,
        paper=paper,
    )
