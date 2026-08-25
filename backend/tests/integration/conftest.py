"""Integration test harness: real PostgreSQL (scratch DB) + fakeredis.

The scratch database is recreated on every session and migrated to head via
Alembic — exactly the path production uses. Tests that require these services
are marked ``integration``; when PostgreSQL is unreachable the suite skips
with a clear reason instead of failing.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]

TEST_DATABASE_URL: str = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://forex:change-me-dev-only@localhost:5432/forex_ai_test",
)

# App-under-test settings must resolve to the scratch DB so CLI paths stay
# inside the sandbox (env vars take precedence over .env files).
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

TABLES: tuple[str, ...] = (
    "audit_log",
    "refresh_tokens",
    "system_settings",
    "users",
    "candles",
    "instruments",
    "provider_health",
)


def _root_url() -> str:
    return TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"


def _db_name() -> str:
    return TEST_DATABASE_URL.rsplit("/", 1)[1]


async def _postgres_reachable() -> bool:
    try:
        import asyncpg

        dsn = _root_url().replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(dsn, timeout=3)
        await conn.close()
        return True
    except Exception:  # noqa: BLE001 - any failure means "unavailable"
        return False


@pytest.fixture(scope="session")
def pg_available() -> Iterator[bool]:
    reachable = asyncio.run(_postgres_reachable())
    if not reachable:
        pytest.skip(
            "PostgreSQL unavailable — start datastores via `docker compose up -d postgres redis`"
        )
    yield True


@pytest.fixture(scope="session")
def pg_engine(pg_available: bool) -> Iterator[AsyncEngine]:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    async def run_sql(statement: str) -> None:
        root_engine = create_async_engine(_root_url(), isolation_level="AUTOCOMMIT")
        try:
            async with root_engine.connect() as conn:
                await conn.execute(text(statement))
        finally:
            await root_engine.dispose()

    asyncio.run(run_sql(f'DROP DATABASE IF EXISTS "{_db_name()}" WITH (FORCE)'))
    asyncio.run(run_sql(f'CREATE DATABASE "{_db_name()}"'))

    alembic_cfg = AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    os.environ["ALEMBIC_DATABASE_URL"] = TEST_DATABASE_URL
    # Alembic's env.py calls asyncio.run(); execute it outside the session loop.
    asyncio.run(asyncio.to_thread(command.upgrade, alembic_cfg, "head"))

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    yield engine
    asyncio.run(engine.dispose())
    asyncio.run(run_sql(f'DROP DATABASE IF EXISTS "{_db_name()}" WITH (FORCE)'))


@pytest_asyncio.fixture(autouse=True)
async def _isolation(pg_engine: AsyncEngine) -> AsyncIterator[None]:
    """Truncate tables + reset rate-limiter state around each test."""
    from app.api.deps import auth_limiter

    auth_limiter.reset()
    truncate = ", ".join(TABLES)
    async with pg_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {truncate} RESTART IDENTITY CASCADE"))  # noqa: S608
    yield
    auth_limiter.reset()


@pytest_asyncio.fixture()
async def db_sessionmaker(
    pg_engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield async_sessionmaker(bind=pg_engine, expire_on_commit=False)


@pytest_asyncio.fixture()
async def fake_redis() -> AsyncIterator[Any]:
    from fakeredis.aioredis import FakeRedis

    client = FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture()
async def client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_redis: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client over the real app with DB/Redis dependencies overridden."""
    from app.core.config import reset_settings_cache
    from app.db.session import get_db, get_redis
    from app.main import create_app
    from httpx import ASGITransport

    reset_settings_cache()
    application = create_app()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        session = db_sessionmaker()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def override_get_redis() -> AsyncIterator[Any]:
        yield fake_redis

    application.dependency_overrides[get_db] = override_get_db
    application.dependency_overrides[get_redis] = override_get_redis

    async with httpx.AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as http:
        yield http


async def register_and_login(
    client: httpx.AsyncClient,
    email: str,
    password: str = "correct-horse-battery-staple",
) -> dict[str, Any]:
    """Register then login via HTTP; returns the TokenOut body."""
    reg = await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert reg.status_code == 201, reg.text
    login = await client.post("/api/v1/auth/login", data={"username": email, "password": password})
    assert login.status_code == 200, login.text
    return login.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
