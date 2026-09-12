# Forex AI System

A production-quality **multi-agent Forex analysis system** that operates exclusively in
**paper-trading / backtesting mode**.

> ⚠️ **SAFE MODE — read this first**
> This system never connects to a brokerage and cannot place live orders.
> Live order execution does not exist anywhere in this codebase — not as a disabled
> feature, not behind a flag. The backend refuses to boot unless `TRADING_MODE=safe`.
> See [`docs/safe-mode.md`](docs/safe-mode.md) and [`SECURITY.md`](SECURITY.md).

## What it is

| Layer | Technology |
|---|---|
| Backend API | Python 3.12 · FastAPI · SQLAlchemy 2 (async) |
| Agents | Technical · Regime · Fundamental/News · Sentiment · Risk · Orchestrator |
| Datastores | PostgreSQL 16 · Redis 7 (cache + streams/pub-sub) |
| Frontend | Next.js 15 · TypeScript · Tailwind CSS |
| Runtime | Docker Compose · NGINX (prod) |
| LLM | OpenCode Zen / Ox Alpha Free via a swappable abstraction, with deterministic fallbacks |

Full architecture, stack rationale, and the phased delivery plan live in
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md). Design decisions are recorded in
[`docs/adr/`](docs/adr/).

## Project status

Phases follow `IMPLEMENTATION_PLAN.md` §14. Each phase is verified before the next begins.

| Phase | Scope | Status |
|---|---|---|
| 0–9 | Foundation, backend core, market data, agents, decision pipeline, backtesting, monitoring, alerts, frontend | ✅ done |
| 10 | Security, performance & release hardening | ✅ done — accepted 2026-09-12 (`docs/phase10-final-audit.md`) |
| 11 | Documentation & GitHub readiness — `v0.1.0` release prep | 🔶 in progress |
| 12 | Observability & data-source truth (candles/latest-prices API, per-process exporters) | ✅ done |
| — | Paper-executor loop, frontend portfolio & charts, live trading | ⏸️ deferred by design (SAFE MODE) |

Current release target: **v0.1.0** (backend `0.1.0`, frontend `0.1.0`). Tag creation and
publication are future steps requiring explicit operator approval. Live tracker:
[`PROJECT_STATUS.md`](PROJECT_STATUS.md).

## Quickstart

Prerequisites (all inside WSL2 Ubuntu 24.04): Docker Engine + Compose v2 plugin,
GNU make, Python 3.12, Node.js ≥ 22.

```bash
cp .env.example .env   # then edit secrets — never commit .env
make dev               # builds & starts postgres, redis, api, web (+ prometheus/grafana)
```

## Service addresses (dev)

| Service | Address |
|---|---|
| Dashboard (web) | http://localhost:3000 (frontend port 3000; persistent SAFE MODE badge) |
| API | http://localhost:8000 |
| API docs (OpenAPI) | http://localhost:8000/docs |
| Health/liveness | http://localhost:8000/health/live |
| Readiness | http://localhost:8000/health/ready → `{"status":"ok","mode":"safe"}` |
| System status | http://localhost:8000/system/status |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3001 (dev default `admin`/`admin` — rotate before any wider use; anonymous Viewer on in dev) |

Stop everything with `make dev-down` (add `--delete-volumes` data wipe via `make dev-destroy`).

## Production / staging stack

```bash
make prod-up     # HTTPS edge -> api + web through NGINX, no host DB/Redis ports
make prod-down
```

- HTTPS termination on :443 (HTTP/2, TLS 1.2/1.3) with an HTTP→HTTPS redirect; the only
  host-exposed services are the 443/80 edge.
- Staging uses **self-signed certificates** generated under `infra/certs/` (gitignored).
  **Real-CA TLS and HSTS require a production domain** and are intentionally not enabled
  until then (see `docs/phase10-staging-tls.md`, `docs/phase10-final-audit.md`).
- When serving on `https://localhost`, export `CORS_ORIGINS=https://localhost` for the
  browser WebSocket origin check (default in the overlay is `https://localhost`; a local
  `.env` may override it).

## Development workflow

```bash
make backend-venv        # one-time: backend/.venv + runtime/dev deps (hashed lock)
make verify              # ruff format+lint, mypy, pytest, eslint, tsc  ← run before pushing
```

Backend test targets:

```bash
make test-backend-unit         # unit suite + strict coverage gate (≥90% on the sync core)
make test-backend-integration  # real PostgreSQL/Redis: auth flows, token rotation, RBAC matrix
make migrate-backend           # apply Alembic migrations with host-side DATABASE_URL
```

Frontend test targets:

```bash
make install-frontend   # npm ci
make lint-frontend      # eslint
make typecheck-frontend # tsc --noEmit
make build-frontend     # next build
(cd frontend && npm test)   # vitest unit suite
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
`build-frontend`, `prod-up`, `prod-down`, `backup`, `restore`, `clean`. Run `make help`
for the full list.

Install the git hook integration once per clone:

```bash
pip install pre-commit && pre-commit install
```

## Security

- **SAFE MODE is structural** and enforced at five independent layers
  (config, code, orchestrator, runtime/health, tests) — see [`docs/safe-mode.md`](docs/safe-mode.md).
- Auth: Argon2id password hashing, short-lived access tokens, rotating refresh tokens
  with reuse detection, RBAC, per-IP rate limiting (honors `X-Forwarded-For` only from
  trusted proxy peers).
- Security headers + Content-Security-Policy on API and NGINX responses; secrets
  redacted in logs; `.env` and TLS key material git-ignored.
- Dependency CVE gates in CI: pip-audit, npm-audit policy allow-list, and Trivy
  container image scanning (see `.github/workflows/`).
- Reporting guidance: [`SECURITY.md`](SECURITY.md).
- Threat model & self-audit checklist: [`docs/threat-model.md`](docs/threat-model.md).

## Backup & restore

```bash
make backup            # timestamped gzipped dump + .sha256 under backups/ (gitignored)
make restore FILE=backups/forex_ai-*.sql.gz   # restore into an isolated temp DB; never overwrites live
```

Full procedure, retention, and recovery guidance: [`docs/phase10-backup-restore.md`](docs/phase10-backup-restore.md)
and [`docs/runbook.md`](docs/runbook.md) §7.

## Repository layout

```
backend/    FastAPI app, agents, trading (paper-only), tests
frontend/   Next.js dashboard
infra/      nginx / prometheus / grafana configuration
docs/       architecture, safe-mode contract, ADRs, hardening & audit records
scripts/    backup / restore tooling
```

## License

[MIT](LICENSE).