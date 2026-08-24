"""Management CLI (Typer): createuser, check-db.

Examples::

    python -m app.cli createuser --email admin@example.com --role admin
    python -m app.cli check-db
"""

from __future__ import annotations

import asyncio

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


if __name__ == "__main__":
    app()
