"""Runtime-tunable market universe (symbols/timeframes) stored in system_settings.

Defaults come from environment Settings (ADR-0003); admins may override them
at runtime via the admin API. The ingest worker re-reads this configuration
every cycle, so changes take effect without restarts.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.data.timeframes import Timeframe
from app.models.system_setting import SystemSetting

SYMBOLS_KEY = "market.symbols"
TIMEFRAMES_KEY = "market.timeframes"


def normalize_symbols(symbols: list[str]) -> list[str]:
    cleaned: list[str] = []
    for symbol in symbols:
        normalized = symbol.strip().upper()
        if len(normalized) != 6 or not normalized.isalpha():
            raise ValueError(f"Invalid FX pair {symbol!r}; expected 6-letter form like EURUSD")
        if normalized not in cleaned:
            cleaned.append(normalized)
    if not cleaned:
        raise ValueError("At least one pair is required")
    return cleaned


def normalize_timeframes(timeframes: list[str]) -> list[str]:
    ordered: list[str] = []
    for timeframe in timeframes:
        normalized = timeframe.strip().upper()
        if not Timeframe.is_valid(normalized):
            raise ValueError(f"Unknown timeframe {timeframe!r}; supported: {Timeframe.values()}")
        if normalized not in ordered:
            ordered.append(normalized)
    if not ordered:
        raise ValueError("At least one timeframe is required")
    return sorted(ordered, key=Timeframe.rank)


async def _read_setting(session: AsyncSession, key: str) -> list[str] | None:
    row = await session.get(SystemSetting, key)
    if row is None:
        return None
    value = row.value.get("value") if isinstance(row.value, dict) else None
    return value if isinstance(value, list) else None


async def get_market_config(
    session: AsyncSession, settings: Settings
) -> tuple[list[str], list[str]]:
    """Effective (symbols, timeframes); DB overrides beat env defaults."""
    symbols = await _read_setting(session, SYMBOLS_KEY)
    timeframes = await _read_setting(session, TIMEFRAMES_KEY)
    effective_symbols = symbols if symbols is not None else settings.market_symbols
    effective_timeframes = timeframes if timeframes is not None else settings.market_timeframes
    return effective_symbols, effective_timeframes


async def set_market_config(
    session: AsyncSession,
    *,
    actor: str,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Persist overrides; returns the resulting effective config."""
    current_symbols, current_timeframes = await get_market_config(session, get_settings_safe())
    new_symbols = normalize_symbols(symbols) if symbols is not None else current_symbols
    new_timeframes = (
        normalize_timeframes(timeframes) if timeframes is not None else current_timeframes
    )

    for key, values in ((SYMBOLS_KEY, new_symbols), (TIMEFRAMES_KEY, new_timeframes)):
        existing = await session.get(SystemSetting, key)
        if existing is None:
            session.add(SystemSetting(key=key, value={"value": values}, updated_by_user_id=actor))
        else:
            existing.value = {"value": values}
            existing.updated_by_user_id = actor
    await session.flush()
    return new_symbols, new_timeframes


def get_settings_safe() -> Settings:
    from app.core.config import get_settings

    return get_settings()
