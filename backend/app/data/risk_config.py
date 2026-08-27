"""Runtime-tunable risk parameters (Phase 5).

Defaults come from environment Settings; admins may override individual knobs
at runtime via the admin API (persisted in ``system_settings``). The
orchestrator re-reads this configuration every cycle, so changes take effect
without restarts — mirroring ``market_config``.

SAFE MODE: risk parameters gate *paper analysis intents* only; none of these
values can enable live execution. Every knob is range-validated during load so
a malformed override degrades to the safe default rather than an unsafe one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.core.config import Settings
from app.models.system_setting import SystemSetting

RISK_SETTING_KEY: str = "risk.params"

#: Keys that may be overridden at runtime, with (parser, bounds-checker).
_EDITABLE: dict[str, tuple[Any, Any]] = {
    "max_risk_pct_account": (float, (0.0, 1.0)),
    "max_exposure_pct": (float, (0.0, 1.0)),
    "max_daily_loss_pct": (float, (0.0, 1.0)),
    "max_drawdown_pct": (float, (0.0, 1.0)),
    "min_rr": (float, (0.0, None)),
    "sl_atr_multiple": (float, (0.0, None)),
    "tp_atr_multiple": (float, (0.0, None)),
    "vol_target_pct": (float, (0.0, 1.0)),
    "correlation_cap_pct": (float, (0.0, 1.0)),
}


@dataclass(frozen=True, slots=True)
class RiskParams:
    """Resolved risk configuration (frozen snapshot per cycle)."""

    max_risk_pct_account: float
    max_exposure_pct: float
    max_daily_loss_pct: float
    max_drawdown_pct: float
    min_rr: float
    sl_atr_multiple: float
    tp_atr_multiple: float
    vol_target_pct: float
    correlation_cap_pct: float
    risk_enabled: bool
    paper_equity: float

    @classmethod
    def from_settings(cls, settings: Settings) -> RiskParams:
        return cls(
            max_risk_pct_account=settings.risk_max_risk_pct_account,
            max_exposure_pct=settings.risk_max_exposure_pct,
            max_daily_loss_pct=settings.risk_max_daily_loss_pct,
            max_drawdown_pct=settings.risk_max_drawdown_pct,
            min_rr=settings.risk_min_rr,
            sl_atr_multiple=settings.risk_sl_atr_multiple,
            tp_atr_multiple=settings.risk_tp_atr_multiple,
            vol_target_pct=settings.risk_vol_target_pct,
            correlation_cap_pct=settings.risk_correlation_cap_pct,
            risk_enabled=settings.risk_enabled,
            paper_equity=settings.risk_paper_equity,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamped(value: Any, bounds: tuple[Any, ...], default: Any) -> Any:
    """Parse a raw override and clamp into safe bounds; default on failure."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    lo, hi = bounds
    if lo is not None and parsed < lo:
        return default
    if hi is not None and parsed > hi:
        return default
    return parsed


async def load_risk_params(session: Any, settings: Settings) -> RiskParams:
    """Resolve risk params: env defaults overlaid by any stored overrides.

    Malformed/out-of-range overrides degrade to the env default (fail closed).
    """
    base = RiskParams.from_settings(settings)
    row = await session.get(SystemSetting, RISK_SETTING_KEY)
    if row is None or not isinstance(row.value, dict):
        return base
    overrides = row.value
    editable = dict(base.to_dict())
    for key, (parser, bounds) in _EDITABLE.items():  # noqa: B007 - parser reserved
        if key in overrides:
            default = getattr(base, key)
            editable[key] = _clamped(overrides[key], bounds, default)
    return RiskParams(**editable)


async def set_risk_params(session: Any, *, actor: str, updates: dict[str, Any]) -> RiskParams:
    """Apply runtime overrides; returns the resulting resolved snapshot."""
    from app.core.config import get_settings

    base = await load_risk_params(session, get_settings())
    editable = dict(base.to_dict())
    for key, (_, bounds) in _EDITABLE.items():
        if key in updates:
            default = getattr(base, key)
            editable[key] = _clamped(updates[key], bounds, default)
    result = RiskParams(**editable)

    existing = await session.get(SystemSetting, RISK_SETTING_KEY)
    stored = {key: getattr(result, key) for key in _EDITABLE}
    if existing is None:
        session.add(
            SystemSetting(
                key=RISK_SETTING_KEY,
                value=stored,
                description="Runtime risk parameter overrides",
                updated_by_user_id=actor,
            )
        )
    else:
        existing.value = stored
        existing.updated_by_user_id = actor
    await session.flush()
    return result
