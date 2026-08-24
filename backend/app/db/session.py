"""Async engine/session management.

Accessors are lazily cached so that (a) ASGI test transports which bypass
lifespan still resolve dependencies, and (b) tests can override the FastAPI
dependencies (``get_db``/``get_redis``) with their own factories.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from redis.asyncio import ConnectionPool, Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_engine(database_url: str | None = None) -> AsyncEngine:
    """Return the cached application engine (created on first use).

    ``database_url`` defaults to the configured settings value; passing an
    explicit URL (tests/CLI) creates a separate cached engine for it.
    """
    return create_async_engine(
        database_url or get_settings().database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


@lru_cache(maxsize=1)
def get_sessionmaker(database_url: str | None = None) -> async_sessionmaker[AsyncSession]:
    """Return the cached session factory bound to the application engine."""
    return async_sessionmaker(bind=get_engine(database_url), expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an ORM session with commit-on-success."""
    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@lru_cache(maxsize=1)
def get_redis_client(redis_url: str | None = None) -> Redis:
    """Return a cached Redis client over a lazily-created connection pool."""
    pool = ConnectionPool.from_url(redis_url or get_settings().redis_url, decode_responses=True)
    return Redis(connection_pool=pool)


async def get_redis() -> AsyncIterator[Redis]:
    """FastAPI dependency yielding the shared Redis client."""
    client = get_redis_client()
    try:
        yield client
    finally:
        pass  # pooled client is process-wide; closed during app shutdown


async def dispose_engine() -> None:
    """Dispose the cached engine/session factory (app shutdown / tests)."""
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


async def dispose_redis() -> None:
    """Close the cached Redis client and drop its pool (app shutdown / tests)."""
    client = get_redis_client()
    await client.aclose()
    get_redis_client.cache_clear()
