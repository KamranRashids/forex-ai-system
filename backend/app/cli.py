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


backtest_app = typer.Typer(
    no_args_is_help=True,
    help="Run and inspect backtests (SAFE MODE: analysis only; never creates live orders).",
)


@backtest_app.command("run")
def backtest_run(
    range_start: str = typer.Option(..., "--range", help="Range start..end (ISO-8601 UTC)."),
    pairs: str = typer.Option(..., "--pairs", help="Comma-separated pairs, e.g. EURUSD,GBPUSD."),
    timeframes: str = typer.Option("H1", help="Comma-separated timeframes (M5,M15,H1,H4,D1)."),
    seed: int = typer.Option(0, "--seed", help="Deterministic shuffle/seed."),
    start_equity: float = typer.Option(100000.0, "--equity", help="Starting paper equity."),
    warmup: int = typer.Option(80, "--warmup", help="Warmup bars before decisions begin."),
) -> None:
    """Run a deterministic synthetic-data backtest and persist the results."""
    try:
        start_raw, end_raw = [p.strip() for p in range_start.split("..")]
    except ValueError:
        typer.secho("error: --range must be START..END", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None

    from app.backtest.service import config_from_dict, run_backtest

    settings = get_settings()
    symbols = [s.strip().upper() for s in pairs.split(",") if s.strip()]
    tfs = [t.strip().upper() for t in timeframes.split(",") if t.strip()]
    cfg = config_from_dict(
        {
            "symbols": symbols,
            "timeframes": tfs,
            "start": start_raw,
            "end": end_raw,
            "seed": seed,
            "start_equity": start_equity,
            "warmup_bars": warmup,
        }
    )

    configure_logging_quiet()

    async def run() -> str:
        run_id = await run_backtest(
            cfg=cfg,
            settings=settings,
            session_factory=get_sessionmaker(settings.database_url),
        )
        return str(run_id)

    try:
        run_id = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - CLI surface
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(f"backtest run {run_id} persisted", fg=typer.colors.GREEN)


@backtest_app.command("list")
def backtest_list(limit: int = typer.Option(20, "--limit", help="Number of runs to list.")) -> None:
    """List recent backtest runs (read-only)."""
    from app.backtest.repository import list_runs

    async def run() -> list[str]:
        async with get_sessionmaker(get_settings().database_url)() as session:
            rows = await list_runs(session, limit=limit)
            return [
                (
                    f"{r.id}  {r.status}  {r.started_at.isoformat()}  "
                    f"net={r.metrics.get('net_pnl', 0):.2f}"
                )
                for r in rows
            ]

    try:
        lines = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - CLI surface
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    for line in lines:
        typer.secho(line, fg=typer.colors.GREEN if "COMPLETED" in line else typer.colors.YELLOW)


@backtest_app.command("show")
def backtest_show(run_id: str = typer.Argument(..., help="Backtest run UUID.")) -> None:
    """Show the persisted result summary for a backtest run (read-only)."""
    import uuid as _uuid

    from app.backtest.repository import get_run

    async def run() -> None:
        async with get_sessionmaker(get_settings().database_url)() as session:
            row = await get_run(session, _uuid.UUID(run_id))
            if row is None:
                raise RuntimeError(f"run {run_id} not found")
            typer.secho(f"run {row.id} status={row.status} seed={row.seed}")
            for key, value in row.metrics.items():
                typer.secho(f"  {key}: {value}")

    try:
        asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - CLI surface
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


app.add_typer(backtest_app, name="backtest")


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
