"""Integration: health probes, system status matrix, migrations consistency, CLI."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration]


@pytest.mark.asyncio
async def test_health_live_reports_safe_mode(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "mode": "safe"}


@pytest.mark.asyncio
async def test_health_ready_all_green(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "mode": "safe"}


@pytest.mark.asyncio
async def test_system_status_matrix(client: httpx.AsyncClient) -> None:
    resp = await client.get("/system/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "forex-ai-api"
    assert body["safe_mode"] is True
    assert body["trading_mode"] == "safe"
    components = body["components"]
    for name in ("database", "redis", "migrations", "safe_mode"):
        assert name in components, f"missing component {name}"
        assert components[name]["ok"] is True, f"{name} reported not ok: {components[name]}"
    assert "latency_ms" in components["database"]


@pytest.mark.safety
@pytest.mark.asyncio
async def test_ready_fails_when_safe_mode_absent(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SAFE MODE is a readiness component: an invalid mode must fail readiness."""
    from app.core.config import reset_settings_cache

    monkeypatch.setenv("TRADING_MODE", "live")
    reset_settings_cache()
    # Settings validation itself refuses to construct; the process cannot even
    # boot with a bad mode, so readiness would never be reached. Verify that.
    from app.core.config import get_settings

    with pytest.raises(Exception, match="Refusing to start"):
        _ = get_settings()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_migrations_match_models(pg_engine: AsyncEngine) -> None:
    """The applied schema must equal the declared SQLAlchemy metadata."""
    from app.models import AuditLog, RefreshToken, SystemSetting, User  # noqa: F401
    from sqlalchemy import inspect as sa_inspect

    def compare(sync_conn: object) -> None:
        inspector = sa_inspect(sync_conn)
        db_tables = set(inspector.get_table_names())
        expected_tables = {"users", "refresh_tokens", "audit_log", "system_settings"}
        assert expected_tables <= db_tables

        for model in (User, RefreshToken, AuditLog, SystemSetting):
            table = model.__table__
            db_columns = {c["name"]: c for c in inspector.get_columns(table.name)}
            for column in table.columns:
                assert column.name in db_columns, (
                    f"{table.name}.{column.name} missing from database"
                )
            db_pk = set(inspector.get_pk_constraint(table.name)["constrained_columns"])
            expected_pk = {c.name for c in table.primary_key.columns}
            assert db_pk == expected_pk, f"{table.name} PK mismatch: {db_pk} != {expected_pk}"

    async with pg_engine.connect() as conn:
        await conn.run_sync(compare)


@pytest.mark.asyncio
async def test_alembic_version_is_head(pg_engine: AsyncEngine) -> None:
    from pathlib import Path

    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    backend_root = Path(__file__).resolve().parents[2]
    script = ScriptDirectory.from_config(AlembicConfig(str(backend_root / "alembic.ini")))
    heads = set(script.get_heads())

    async with pg_engine.connect() as conn:
        current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    assert current in heads


@pytest.mark.integration
def test_cli_createuser_and_check_db(pg_available: bool) -> None:
    """CLI createuser works against the migrated scratch DB (bootstrap admin)."""
    from app.cli import app as cli_app
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "createuser",
            "--email",
            "cli-admin@example.com",
            "--password",
            "cli-created-password-123",
            "--role",
            "viewer",  # viewer requested on empty DB -> bootstrap guard forces admin
        ],
    )
    assert result.exit_code == 0, result.output
    assert "role=admin" in result.output

    check = runner.invoke(cli_app, ["check-db"])
    assert check.exit_code == 0, check.output
    assert "database ok" in check.output


@pytest.mark.asyncio
async def test_metrics_expose_request_counters(client: httpx.AsyncClient) -> None:
    await client.get("/health/live")
    metrics = (await client.get("/metrics")).text
    assert "http_requests_total" in metrics
