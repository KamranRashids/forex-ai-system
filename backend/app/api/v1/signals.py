"""Signals read API (Phase 3 minimal surface; WS hub arrives in Phase 8)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DBSession
from app.data.signal_repository import (
    load_history,
    load_latest_per_agent,
    row_to_signal,
)
from app.schemas.signals import SignalOut

router = APIRouter(prefix="/signals", tags=["signals"])

_TIMEFRAME_PATTERN = "^(M5|M15|H1|H4|D1)$"


def _to_out(row: object) -> SignalOut:
    signal = row_to_signal(row)  # type: ignore[arg-type]
    return SignalOut.model_validate(signal, from_attributes=True)


@router.get("/latest")
async def latest_signals(
    session: DBSession,
    current: CurrentUser,
    symbol: Annotated[str, Query(min_length=6, max_length=12)],
    timeframe: Annotated[str, Query(pattern=_TIMEFRAME_PATTERN)],
    fresh_only: bool = True,
) -> list[SignalOut]:
    """Newest stored signal per agent for a pair/timeframe (viewer+)."""
    rows = await load_latest_per_agent(
        session,
        symbol=symbol,
        timeframe=timeframe.upper(),
        now=datetime.now(UTC) if fresh_only else None,
    )
    return [_to_out(r) for r in rows]


@router.get("")
async def signal_history(
    session: DBSession,
    current: CurrentUser,
    symbol: Annotated[str, Query(min_length=6, max_length=12)],
    timeframe: Annotated[str, Query(pattern=_TIMEFRAME_PATTERN)],
    agent_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    order: Literal["asc", "desc"] = "desc",
) -> list[SignalOut]:
    """Recent signal history for a pair/timeframe (viewer+), newest first."""
    rows = await load_history(
        session,
        symbol=symbol,
        timeframe=timeframe.upper(),
        limit=limit,
        agent_id=agent_id,
    )
    outs = [_to_out(r) for r in rows]
    if order == "asc":
        outs.reverse()
    return outs
