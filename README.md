# Forex AI System

A production-quality **multi-agent Forex analysis system** that operates exclusively in
**paper-trading / backtesting mode**.

> ⚠️ **SAFE MODE — read this first**
> This system never connects to a brokerage and cannot place live orders.
> Live order execution does not exist anywhere in this codebase — not as a disabled
> feature, not behind a flag. The backend refuses to boot unless `TRADING_MODE=safe`.
> See [`docs/safe-mode.md`](docs/safe-mode.md).

## What it is

| Layer | Technology |
|---|---|
| Backend API | Python 3.12 · FastAPI · SQLAlchemy 2 (async) |
| Agents | Technical · Regime · Fundamental/News · Sentiment · Risk · Orchestrator |
| Datastores | PostgreSQL 16 · Redis 7 (cache + streams/pub-sub) |
| Frontend | Next.js 15 · TypeScript · Tailwind CSS · Lightweight Charts |
| Runtime | Docker Compose · NGINX (prod) |
| LLM | OpenCode Zen / Ox Alpha Free via a swappable abstraction, with deterministic fallbacks |

Full architecture, stack rationale, and the phased delivery plan live in
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md). Design decisions are recorded in
[`docs/adr/`](docs/adr/).

## Quickstart

Prerequisites (all inside WSL2 Ubuntu 24.04): Docker Engine + Compose v2 plugin,
GNU make, Python 3.12, Node.js ≥ 22.

```bash
cp .env.example .env   # then edit secrets — never commit .env
make dev               # builds & starts postgres, redis, api, web
```

Then open:

- Dashboard: http://localhost:3000 (shows the persistent SAFE MODE badge)
- API docs: http://localhost:8000/docs
- Liveness: http://localhost:8000/health/live

Stop everything with `make dev-down` (add `--delete-volumes` data wipe via `make dev-destroy`).

## Development workflow

```bash
make backend-venv        # one-time: backend/.venv + runtime/dev deps
make verify              # ruff format+lint, mypy, pytest, eslint, tsc  ← run before pushing
```

Backend test targets:

```bash
make test-backend-unit         # unit suite + strict coverage gate (≥90% on the sync core)
make test-backend-integration  # real PostgreSQL/Redis: auth flows, token rotation, RBAC matrix
make migrate-backend           # apply Alembic migrations with host-side DATABASE_URL
```

Create the first account (the bootstrap admin — the API's register endpoint also
promotes the first user to admin automatically):

```bash
docker compose exec api python -m app.cli createuser --email you@example.com --role admin
curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -d 'username=you@example.com&password=...'   # OAuth2 password flow → bearer token
```

Useful targets: `logs`, `ps`, `format-backend`, `lint-backend`, `typecheck-backend`,
`test-backend`, `test-backend-unit`, `test-backend-integration`, `coverage-backend`,
`migrate-backend`, `install-frontend`, `lint-frontend`, `typecheck-frontend`,
`build-frontend`, `prod-up`, `prod-down`, `clean`. Run `make help` for the full list.

Install the git hook integration once per clone:

```bash
pip install pre-commit && pre-commit install
```

## Project status

Phases follow `IMPLEMENTATION_PLAN.md` §14. Each phase is verified before the next begins.

| Phase | Scope | Status |
|---|---|---|
| 0 | Repository foundation & infra scaffold | ✅ done |
| 1 | Backend core (config, DB, auth, health) | ✅ done |
| 2 | Market data layer (providers, ingest worker, candles) | ✅ done |
| 3 | Agent framework + technical & regime agents | ✅ done |
| 4 | Fundamental & sentiment agents | ⏳ next |
| 5–11 | Risk/orchestrator/paper executor → backtesting → API → frontend → hardening → release | ⏳ pending |

## Repository layout

```
backend/    FastAPI app, agents, trading (paper-only), tests
frontend/   Next.js dashboard
infra/      nginx / prometheus / grafana configuration
docs/       architecture, safe-mode contract, ADRs
```

## License

[MIT](LICENSE).
