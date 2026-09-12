# Changelog

All notable changes to the Multi-Agent Forex AI System are recorded here.
The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-12

First release of the Multi-Agent Forex AI System — a **SAFE MODE only**
publication. The system performs paper trading / analysis exclusively: it never
connects to a brokerage and cannot place live orders. Live order execution does
not exist anywhere in the codebase — not as a disabled feature, not behind a flag.
The backend refuses to boot unless `TRADING_MODE=safe`.

### Added

- **Backend core** (Phase 1): FastAPI application, Pydantic-settings config with
  structural SAFE MODE enforcement, SQLAlchemy 2 async + Alembic migrations
  (head `0007`), Argon2id password hashing, JWT access/refresh with rotation and
  reuse detection, RBAC (viewer < trader < admin), RFC 7807 error contracts,
  correlation IDs, log secret-redaction, health/liveness endpoints.
- **Market data layer** (Phase 2): provider-independent interface (ADR-0003) with
  deterministic `synthetic` provider (default) and OANDA practice adapter; ingest
  worker with gap detection, staleness checks, and upsert-on-close candle
  persistence; FX-week market model.
- **Agent framework** (Phase 3): `BaseAgent` contract; technical agent (nine
  documented indicator votes, ATR dead-zones, ADX trend-gating) and regime agent
  (trend/volatility/session conditioning).
- **Fundamental & sentiment** (Phase 4): provider-independent news and economic
  calendar ingestion (synthetic default, Finnhub when configured) with idempotent
  dedup; fundamental risk-state scoring and finance-lexicon sentiment agent;
  optional LLM reasoning with deterministic fallbacks behind `LLMClient`
  (ADR-0004).
- **Decision pipeline** (Phase 5): single-owner orchestrator producing fully
  risk-gated PAPER intents; risk engine (per-trade/exposure/correlated/drawdown
  caps) and decision audit trail.
- **Backtesting engine** (Phase 6): deterministic replay reusing the live agent
  and risk code paths, with CLI, API, and results/equity persistence.
- **Monitoring & runtime hardening** (Phase 7): truthful per-worker healthchecks,
  worker heartbeat/loop metrics, Prometheus scraping, Grafana runtime dashboard.
- **Alerts** (Phase 8/9): durable alerts with admin acknowledgement, authenticated
  REST + WebSocket live feed with reconnect handling.
- **Observability & data-source truth** (Phase 12): candles + latest prices API;
  per-process worker Prometheus exporters on 9101–9105; truthful container probes.
- **Frontend** (Phase 9): Next.js 15 dashboard — sign-in, SAFe-mode banner,
  signals & decisions view, live alerts view, backtest results & strategy
  analysis, candle source display; Vitest unit tests (66).
- **Backup/restore tooling**: `backups/backup_db.sh`, `restore_db.sh`,
  `make backup` / `make restore` with SHA-256 checksums and isolated temp-DB
  restore (never overwrites the live database).

### Security

- **SAFE MODE enforced at five independent layers** (config, code, orchestrator,
  runtime/health, tests) — see `docs/safe-mode.md`. Removing it requires touching
  all five plus a signed governance change.
- Argon2id password hashing; short-lived access tokens; rotating refresh tokens
  with reuse detection; HttpOnly/SameSite refresh cookies (Secure in prod).
- Per-IP sliding-window rate limiting on register/login/refresh, honoring
  `X-Forwarded-For`/`X-Real-IP` only from trusted proxy peers (spoof-proof by
  default, trust scope pinned to the prod overlay subnet).
- Security headers + Content-Security-Policy on API and nginx responses,
  verified against Next.js dev/standalone.
- Secret hygiene: `.env` and TLS key material git-ignored, secrets redacted in
  logs, credentials never baked into container images.
- Dependency CVE gates in CI: `pip-audit` (runtime closure, exit 0 currently),
  `npm audit` policy gate with an explicit allow-list, and Trivy container image
  scanning (digest-pinned) of the exact built images.
- Reproducible, hash-pinned Python lock files (`requirements.lock`, 56 pins /
  1174 hashes; `requirements-dev.lock`, 72 pins / 1518 hashes) installed with
  `--require-hashes` in images, CI, and `make backend-venv`.
- Frontend `sharp` updated to 0.35.4 (resolves prior advisory; no exceptions
  remain for sharp). Base image OpenSSL updated for the resolved advisory.
- Unauthenticated `/metrics` and Grafana `admin`/`admin` + anonymous Viewer are
  documented **dev-stack defaults**; operators must override before any wider
  deployment.

### Testing

- Backend unit suite: 463 tests, 92.61% coverage (strict ≥90% gate), including
  the `safety`-marked SAFE MODE regression tests.
- Backend integration suite: 102 tests against real PostgreSQL (scratch DB via
  Alembic) and Redis — auth flows, token rotation, RBAC permission matrix.
- Frontend: Vitest 66/66; ESLint, `tsc --noEmit`, and `next build` clean.
- `ruff check`, `ruff format --check`, and `mypy` (strict, 137 files) clean.
- Live probe: 11/11 compose services healthy; `/health/ready` =
  `{"status":"ok","mode":"safe"}`; `/system/status` green at migrations `0007`.

### Infrastructure

- Docker Compose development stack (postgres, redis, api, 5 workers, web,
  prometheus, grafana) and production overlay (`docker-compose.prod.yml`):
  optimized multi-stage images, non-root runtime users, no host ports aside from
  the HTTPS/HTTP edge, pinned network subnet, and per-service resource limits.
- NGINX reverse proxy with staging TLS termination (HTTP/2, TLSv1.2/TLSv1.3,
  WebSocket upgrade, 80→301) using git-ignored self-signed staging certificates.
- CI/CD: backend (ruff/mypy/unit+coverage, integration vs real PG/Redis),
  frontend (eslint/tsc/build/npm-audit), and docker-build with Trivy scan — all
  workflow actions SHA-pinned with minimal permissions; Dependabot enabled.

### Documentation

- Architecture design and rationale: `IMPLEMENTATION_PLAN.md`, `docs/architecture.md`.
- Decision records: ADR-0001..0005 (`docs/adr/`).
- SAFE MODE contract: `docs/safe-mode.md`.
- Operations: `docs/runbook.md` (health checks, recovery, backup/restore).
- Security: `docs/threat-model.md`, `docs/dependency-security-audit.md`, and the
  `docs/phase10-*.md` hardening, TLS, backup/restore, and lockfile audit records.
- Release audit: `docs/phase11-release-audit.md`, `docs/phase10-final-audit.md`.

### Known Limitations

- **Live trading is disabled and intentionally not implemented.** SAFE MODE is
  structural; there is no executor worker and no real-broker adapter to enable.
- **Automatic real-money order execution is not enabled** anywhere in the
  codebase (out of scope for v1 by design).
- **Real-CA TLS requires a production domain.** Staging uses git-ignored
  self-signed certificates for `localhost`; public certificates (e.g. Let's
  Encrypt via HTTP-01/DNS-01) are a deployment prerequisite for any real
  hostname, at which point certs replace the staging material in `infra/certs/`.
- **HSTS is not enabled** in staging — deliberately off until real-CA
  certificates are served post-deployment.
- **Paper-executor, portfolio, and charts were intentionally deferred.**
  `PaperBroker` is exercised by the backtester; the orchestrator emits
  risk-gated PAPER intents but no live simulated-fill loop consumes them, and the
  frontend portfolio KPIs/equity/positions and charts pages are not implemented.
- **F-02 (PostCSS, `next` build-time) remains a documented exception** in
  `.trivyignore` / the npm-audit allow-list — no fix exists upstream at release
  time; the allow-list is scoped to those advisories only.
- **F-05 remains documented exceptions:** npm-CLI–vendored `brace-expansion`,
  `ip-address`, and `tar` advisories inside the bundled npm distribution are
  recorded in `.trivyignore` with rationale; they do not affect the application
  runtime.
- **Market timeframes** are configured to M15/H1/H4 by default; M5/D1 are
  supported by the engine but configuration-gated off (by design).
- **Deferred measurement items** (documented in the Phase 10 final audit):
  EXPLAIN pass on hot endpoints, Redis cache-review, WS fan-out benchmark, and a
  secrets-rotation runbook are outstanding future work.

---

## [Unreleased]

- Phase 11 release preparation: v0.1.0 tag cut, status-document refresh, release
  checklist (see `docs/release-checklist.md`).