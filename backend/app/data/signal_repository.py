"""Signal persistence + read access (idempotent, first-writer-wins)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentSignal
from app.models.agent_signal import AgentSignalRow


def _to_row_values(signal: AgentSignal) -> dict[str, object]:
    return {
        "run_id": signal.run_id,
        "agent_id": signal.agent_id,
        "agent_version": signal.version,
        "symbol": signal.symbol.upper(),
        "timeframe": signal.timeframe,
        "direction": signal.direction.value,
        "confidence": round(signal.confidence, 4),
        "bucket_ts": signal.bucket_ts,
        "rationale": signal.rationale,
        "features": signal.features,
        "created_at": signal.created_at,
        "valid_until": signal.valid_until,
    }


async def save_signals(session: AsyncSession, signals: list[AgentSignal]) -> int:
    """Insert signals; replays of the same (agent, symbol, tf, bucket) are no-ops.

    Returns the number of freshly inserted rows.
    """
    inserted = 0
    for signal in signals:
        stmt = (
            pg_insert(AgentSignalRow)
            .values(_to_row_values(signal))
            .on_conflict_do_nothing(index_elements=["agent_id", "symbol", "timeframe", "bucket_ts"])
            .returning(AgentSignalRow.id)
        )
        result = await session.scalar(stmt)
        if result is not None:
            inserted += 1
    return inserted


async def load_latest_per_agent(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    agent_id: str | None = None,
    now: datetime | None = None,
) -> list[AgentSignalRow]:
    """Newest stored row per agent for (symbol, timeframe), optionally fresh-only."""
    conditions = [
        AgentSignalRow.symbol == symbol.upper(),
        AgentSignalRow.timeframe == timeframe,
    ]
    if agent_id:
        conditions.append(AgentSignalRow.agent_id == agent_id)

    rows = (await session.execute(select(AgentSignalRow).where(*conditions))).scalars().all()
    latest: dict[str, AgentSignalRow] = {}
    for row in rows:
        current = latest.get(row.agent_id)
        if current is None or row.created_at >= current.created_at:
            latest[row.agent_id] = row
    ordered = sorted(latest.values(), key=lambda r: r.agent_id)
    if now is not None:
        ordered = [r for r in ordered if r.valid_until is None or r.valid_until >= now]
    return ordered


async def load_history(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    limit: int = 100,
    agent_id: str | None = None,
) -> list[AgentSignalRow]:
    """Most recent signals for a series, newest first."""
    conditions = [
        AgentSignalRow.symbol == symbol.upper(),
        AgentSignalRow.timeframe == timeframe,
    ]
    if agent_id:
        conditions.append(AgentSignalRow.agent_id == agent_id)
    result = await session.execute(
        select(AgentSignalRow)
        .where(*conditions)
        .order_by(AgentSignalRow.created_at.desc(), AgentSignalRow.agent_id)
        .limit(limit)
    )
    return list(result.scalars().all())


def row_to_signal(row: AgentSignalRow) -> AgentSignal:
    """Rehydrate a domain signal from its persisted row."""
    from app.agents.base import Direction

    return AgentSignal(
        agent_id=row.agent_id,
        version=row.agent_version,
        symbol=row.symbol,
        timeframe=row.timeframe,
        direction=Direction(row.direction),
        confidence=float(row.confidence),
        bucket_ts=row.bucket_ts,
        rationale=row.rationale or "",
        features=row.features,
        created_at=row.created_at,
        valid_until=row.valid_until or row.bucket_ts,
        run_id=str(row.run_id),
    )


def new_run_id() -> str:
    """Batch correlation id."""
    return uuid.uuid4().hex
