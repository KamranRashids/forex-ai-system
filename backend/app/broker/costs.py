"""Deterministic transaction cost model: spread + slippage (Phase 6).

The cost model is the same one the future live paper broker will use, so
backtest results are cost-parity with paper trading. It is deterministic:
slippage is a pure function of a seed, direction, and notional — no hidden
entropy, so identical inputs produce identical costs.

SAFE MODE: this is a paper/analysis cost model only. Nothing here places,
routes, or describes a real order.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CostParams:
    """Cost knobs; defaults are configurable per run."""

    #: Spread applied on entry and exit, in the instrument's fractional price.
    #: For an FX pair this is typically a small fraction of a pip.
    spread: float = 0.0001
    #: Fixed commission applied per side per trade (0 for plain FX P&L).
    commission_per_side: float = 0.0
    #: Slippage magnitude expressed as a fraction of price (0 disables).
    slippage_pct: float = 0.00002
    #: Liquidity-sensitive slippage: how much slippage scales with notional.
    slippage_notional_scale: float = 1e-7


def _digest(key: str) -> float:
    """Deterministic uniform draw in [0, 1) from a string key."""
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    (raw,) = struct.unpack(">Q", digest)
    return float(raw) / float(1 << 64)


def slippage_bps(
    *,
    seed: int,
    symbol: str,
    side: str,
    notional: float,
    params: CostParams | None = None,
) -> float:
    """Slippage in basis points for one fill (deterministic).

    Adverse selection is modelled as a symmetric positive slippage proportional
    to the configured ``slippage_pct`` magnitude. A per-(seed, symbol) draw
    keeps it deterministic; a notional volume factor gives larger trades
    slightly better per-unit economics. ``slippage_pct == 0`` disables slippage.
    """
    p = params or CostParams()
    draw = _digest(f"bt|slip|{seed}|{symbol}|{side}")
    volume_factor = 1.0 + abs(notional) * 2.0 * 1e-9
    base = draw * 0.5 / volume_factor
    return max(0.0, base * (p.slippage_pct / 0.00002))


def cost_per_unit(
    *,
    price: float,
    seed: int,
    symbol: str,
    side: str,
    notional: float,
    params: CostParams | None = None,
) -> float:
    """Total adverse cost, in price units, applied to a single fill of one unit."""
    p = params or CostParams()
    spread_cost = p.spread / 2.0  # half-spread each way approximated per fill
    slip = slippage_bps(seed=seed, symbol=symbol, side=side, notional=notional, params=p)
    slip_cost = price * (slip / 10_000.0)
    return spread_cost + slip_cost
