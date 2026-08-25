"""Management CLI (Typer): createuser, check-db.

Examples::

    python -m app.cli createuser --email admin@example.com --role admin
    python -m app.cli check-db
"""

from __future__ import annotations

import asyncio
from datetime import UTC

import typer
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.user import UserRole
from app.services.auth_service import count_users, register_user

app = typer.Typer(no_args_is_help=True, help="Forex AI backend management commands.")


async def _create_user(email: str, password: str, role: UserRole) -> tuple[str, str]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        total_before = await count_users(session)
        # Bootstrap guard: an empty database must yield an admin, otherwise the
        # system has no admin path at all (register defaults later users to viewer).
        effective_role = role if (total_before > 0 or role == UserRole.ADMIN) else UserRole.ADMIN
        user = await register_user(
            session,
            email=email,
            password=password,
            role_override=effective_role,
        )
        await session.commit()
        return str(user.id), user.role.value


@app.command()
def createuser(
    email: str = typer.Option(..., prompt=True, help="Login email."),  # noqa: B008 - Typer idiom
    password: str = typer.Option(  # noqa: B008 - Typer idiom
        ..., prompt=True, hide_input=True, confirmation_prompt=True, help="Password."
    ),
    role: UserRole = typer.Option(UserRole.VIEWER, case_sensitive=False, help="Account role."),  # noqa: B008
) -> None:
    """Create a user account.

    The first account in an empty database is always created as admin,
    regardless of --role, to prevent an admin lockout.
    """
    try:
        user_id, effective_role = asyncio.run(_create_user(email, password, role))
    except Exception as exc:  # noqa: BLE001 - CLI surface
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(f"created user {email} id={user_id} role={effective_role}", fg=typer.colors.GREEN)


@app.command()
def check_db() -> None:
    """Verify database connectivity."""
    settings = get_settings()

    async def run(database_url: str) -> bool:
        sessionmaker = get_sessionmaker(database_url)
        async with sessionmaker() as session:
            result = await session.execute(text("SELECT 1"))
            return bool(result.scalar_one() == 1)

    try:
        ok = asyncio.run(run(settings.database_url))
    except Exception as exc:  # noqa: BLE001 - CLI surface
        typer.secho(f"database unreachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if ok:
        typer.secho("database ok", fg=typer.colors.GREEN)


@app.command()
def backfill(
    pairs: str = typer.Option(..., help="Comma-separated pairs, e.g. EURUSD,GBPUSD."),
    timeframes: str = typer.Option("M15,H1", help="Comma-separated timeframes (M5,M15,H1,H4,D1)."),
    start: str = typer.Option(..., help="Range start (ISO-8601, e.g. 2026-08-01T00:00:00Z)."),
    end: str = typer.Option("", help="Range end (ISO-8601; defaults to now)."),
) -> None:
    """Backfill historical candles from the configured provider into PostgreSQL."""
    from datetime import datetime as _dt

    from app.data.ingest import IngestService, seed_instruments
    from app.data.providers.factory import build_provider
    from app.data.timeframes import Timeframe

    settings = get_settings()
    symbols = [s.strip().upper() for s in pairs.split(",") if s.strip()]
    tfs = [t.strip().upper() for t in timeframes.split(",") if t.strip()]
    for tf in tfs:
        if not Timeframe.is_valid(tf):
            typer.secho(f"error: unsupported timeframe {tf!r}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)

    start_ts = _dt.fromisoformat(start.replace("Z", "+00:00"))
    end_ts = _dt.fromisoformat(end.replace("Z", "+00:00")) if end else _dt.now(UTC)
    if end_ts <= start_ts:
        typer.secho("error: end must be after start", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    configure_logging_quiet()

    async def run() -> list[tuple[str, str, int, int]]:
        provider = build_provider(settings)
        service = IngestService(
            settings=settings,
            session_factory=get_sessionmaker(settings.database_url),
            provider=provider,
        )
        async with get_sessionmaker(settings.database_url)() as session:
            seeded = await seed_instruments(session, symbols)
            await session.commit()
        result = await service.run_backfill(list(seeded.values()), tfs, start=start_ts, end=end_ts)
        return [(r.symbol, r.timeframe, r.inserted, r.gaps_detected) for r in result.results]

    try:
        rows = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - CLI surface
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    for symbol, timeframe, inserted, gaps in rows:
        typer.secho(
            f"{symbol} {timeframe}: +{inserted} bars"
            + (f" ({gaps} provider gaps)" if gaps else ""),
            fg=typer.colors.GREEN,
        )


def configure_logging_quiet() -> None:
    """Keep CLI output readable; warnings+ only."""
    from app.core.logging import configure_logging

    configure_logging(log_level="WARNING", json_logs=False)


if __name__ == "__main__":
    app()
