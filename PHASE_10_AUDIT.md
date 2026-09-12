# Phase 10 Audit

Audit date: 2026-09-10 (UTC). Scope: read-only review of the current repo against the
Phase 10 requirements of `IMPLEMENTATION_PLAN.md` §14 (Security, Performance & Release Hardening).
No files were modified by this audit. Classifications: **COMPLETE / PARTIAL / MISSING / NEEDS VERIFICATION**.

Baseline confirmed live: 11/11 compose services up and healthy (api, postgres, redis, prometheus,
grafana, web, 5 workers); `http://localhost:3000` HTTP 200; `/health/ready` = `{"status":"ok","mode":"safe"}`;
`/system/status` green; migrations at head `0007`; pytest 554/554 (incl. `safety` markers), vitest 66/66,
ruff/mypy/eslint/tsc/next-build clean.

---

## 1. Current Status

| Phase 10 requirement | Status | Summary of evidence |
|---|---|---|
| 1. Threat-model review vs security checklist | MISSING | No threat-model/security doc; plan §12 checklist exists in plan only |
| 2. Dependency audits | PARTIAL | Dependabot (pip/npm/docker/actions) configured; **no pip-audit / npm audit step in CI** |
| 3. CSP/security headers verification | PARTIAL | nginx has X-Frame-Options, X-Content-Type-Options, Referrer-Policy, server_tokens off; **CSP missing; no headers on direct API/dev responses; no verification doc** |
| 4. Rate-limit tuning | PARTIAL | Per-IP sliding window on login/register/refresh works; TTL-able knobs exist; **IP source ignores X-Forwarded-For (proxy-safe only), no tuning baselines recorded** |
| 5. Performance checks (EXPLAIN on hot endpoints) | MISSING | No EXPLAIN artifacts or perf doc; hot tables are indexed (candles PK covers range scans), unverified |
| 6. Redis cache review | MISSING | Redis used for heartbeats/staleness/lock/streams/prices; no cache-review or hit-rate analysis |
| 7. WebSocket fan-out benchmark | MISSING | WS hub implemented; no load/fan-out benchmark exists |
| 8. Production Compose overlay | PARTIAL | `docker-compose.prod.yml` present (prod targets, no host ports, nginx); **no TLS ports/certs, no resource limits** |
| 9. TLS/NGINX production config | PARTIAL | nginx config ready (security headers, WS upgrade, upstreams); **listen 80 only — TLS block, HSTS, certs missing** |
| 10. Backup procedure/script | MISSING | No pg_dump/restore script, no `make backup`, no runbook backup/restore section |
| 11. Production resource limits | MISSING | No `deploy.resources` / mem/cpu limits in any compose file |
| 12. Trivy container image scanning in CI | MISSING | `docker-build.yml` only builds images; no trivy step |
| 13. Pen-test/self-audit checklist | MISSING | No checklist document anywhere |
| 14. Secrets rotation runbook | MISSING | `docs/runbook.md` covers ops/health only; no rotation section |
| 15. SAFE MODE verification | COMPLETE | L1–L5 implemented; live probes + `safety`-marked test suites green |
| 16. Phase 10 hardening sign-off | MISSING | No checklist/sign-off section exists |

---

## 2. Security Review

| Finding | Classification | Evidence |
|---|---|---|
| Auth: Argon2id password hashing | COMPLETE | `backend/app/core/security.py:28,41-49` |
| JWT HS256 short-lived access + long-lived refresh tokens | COMPLETE | `security.py:63-102` |
| Refresh rotation + family reuse-detection (revoke whole family) | COMPLETE | `security.py:81-102`, `app/services/auth_service.py` (integration `test_token_rotation.py`) |
| Refresh cookie HttpOnly + SameSite=Lax, Secure in prod | COMPLETE | `app/api/v1/auth.py:38-46`, `config.cookie_secure` |
| RBAC viewer<trader<admin, strict checks | COMPLETE | `api/deps.py:52-60`, `test_permissions_matrix.py` |
| Per-IP rate limiting (login/register/refresh) | COMPLETE (impl) / PARTIAL (proxy) | `api/deps.py:72-87`, `core/ratelimit.py`; `client_ip` ignores `X-Forwarded-For` |
| Secret redaction in logs (passwords/tokens/keys/cookies) | COMPLETE | `core/logging.py:26-69`; unit tests on redaction |
| `.env` gitignored; example only committed | COMPLETE | `.gitignore:2-4` |
| Prod refuses insecure dev `SECRET_KEY` (fail-closed) | COMPLETE | `config.py:178-185` (+ test) |
| Error surface: RFC 7807 problem+json, no internals leaked | COMPLETE | `core/errors.py` |
| Correlation IDs on requests/logs/errors | COMPLETE | `main.py` middleware, `errors.py:125` |
| CORS allowlist (config-driven) | COMPLETE | `main.py:90-100`, `config.cors_origins` |
| Content-Security-Policy header | MISSING | no `Content-Security-Policy` anywhere in repo |
| Security headers on API responses (X-Content-Type-Options etc.) | MISSING | headers exist only in nginx (prod overlay), not on API server |
| Access token stored in `sessionStorage` (XSS-exposed) | NEEDS VERIFICATION | `frontend/src/lib/auth.ts:22-25` — acceptable v1; mitigate via CSP |
| `/metrics` unauthenticated | NEEDS VERIFICATION | documented dev choice (`infra/prometheus/prometheus.yml:4-5`); decide for prod |
| Grafana `admin/admin` + anonymous Viewer (dev default) | NEEDS VERIFICATION | `docker-compose.yml:313-317`; must rotate before prod |
| Threat model / self-audit checklist artifact | MISSING | no doc exists |

---

## 3. TLS / NGINX Review

| Item | Classification | Evidence |
|---|---|---|
| nginx reverse-proxy config (API strip, WS upgrade, upstreams, security headers, gzip, `server_tokens off`) | COMPLETE | `infra/nginx/nginx.conf` |
| TLS termination (`listen 443 ssl`, certs, `ssl_protocols`, ciphers) | MISSING | nginx.conf line 41: “TLS termination … is added in Phase 10”; listen 80 only |
| HTTP→HTTPS redirect + HSTS | MISSING | not present |
| `X-Forwarded-For`/proto handling by backend | MISSING | `client_ip` reads only `request.client.host` (`api/deps.py:67-69`) |
| Secure cookie under reverse proxy (app sees http:// internally) | NEEDS VERIFICATION | `cookie_secure` = `app_env=="prod"`; is set by app, browser sees proxy scheme |
| Prod overlay wires nginx + api + web (`depends_on: service_healthy`) | COMPLETE | `docker-compose.prod.yml`; only `:80` exposed |

---

## 4. Docker Production Review

| Item | Classification | Evidence |
|---|---|---|
| Multi-stage backend image, non-root prod user, slim base | COMPLETE | `backend/Dockerfile:20-30` (USER appuser uid 10001) |
| Multi-stage frontend image, `next standalone`, non-root | COMPLETE | `frontend/Dockerfile:18-34` (USER node) |
| Dev images bind-mount source (reload) | COMPLETE | compose volumes; Dockerfile dev targets |
| Prod overlay: no host DB/Redis ports, no host api/web ports | COMPLETE | `docker-compose.prod.yml` |
| Migrations run before uvicorn on boot | COMPLETE | Dockerfile CMD `alembic upgrade head && uvicorn …` |
| Image scanning (trivy) in CI | MISSING | `docker-build.yml` builds only |
| Base-image digest pinning | MISSING | images are version-tagged (`python:3.12-slim`, `node:24-alpine`, `nginx:1.27-alpine`) not digest-pinned |
| Resource limits (mem/cpu) | MISSING | no `deploy.resources`/`mem_limit` anywhere |
| Non-root in dev stack (runs as root for bind-mounts) | NEEDS VERIFICATION | deliberate (`backend/Dockerfile:33`); dev-only, acceptable |

---

## 5. CI / Supply Chain Review

| Item | Classification | Evidence |
|---|---|---|
| Backend CI: ruff, mypy, unit+cov gate, integration vs real PG/Redis | COMPLETE | `.github/workflows/backend-ci.yml` |
| Frontend CI: eslint, tsc, vitest?, next build | PARTIAL | `frontend-ci.yml` runs eslint+tsc+build; **vitest not executed in CI** |
| Docker build CI (both prod images) | COMPLETE | `docker-build.yml` |
| Dependabot for pip/npm/docker/github-actions | COMPLETE | `.github/dependabot.yml` |
| pip-audit / npm-audit / trivy scan in CI | MISSING | absent |
| SAFE MODE regression in CI (safety-marked tests) | COMPLETE | `safety` marker used; backend-ci runs unit suite incl. them |

---

## 6. Dependency Audit

| Item | Classification | Evidence |
|---|---|---|
| Runtime deps version-bounded | COMPLETE | `pyproject.toml` ranges; frontend `package.json` ranges + lockfile |
| Python CVE scan (pip-audit/safety) | MISSING | not installed, not in CI |
| npm ecosystem audit | MISSING | no `npm audit` step |
| Container CVE scan (trivy) | MISSING | see §5 |
| Dependabot alerts (automated) | COMPLETE | dependabot.yml (weekly npm/pip, monthly docker/actions) |

---

## 7. Performance Review

| Item | Classification | Evidence |
|---|---|---|
| Hot-table indexes present (candles PK = instrument_id+timeframe+ts; users.email unique; refresh_tokens jti unique; alerts/decisions/backtests indexed) | COMPLETE | `migrations/versions/0001-0007` |
| EXPLAIN pass on hot endpoints (candles, signals, decisions, system/status) recorded | MISSING | no artifacts |
| Readiness/status endpoint latency (measured) | COMPLETE | `/system/status` db 3ms, redis 0.5ms (live) — ad-hoc, not documented |
| Redis cache-review / hit-rate analysis | MISSING | Redis usage is state/channel-based; no caching layer review |
| WS fan-out benchmark | MISSING | `app/ws/` hub exists; no load test tooling (locust optional in plan §6.2, not installed) |
| Rate-limit cost | NEEDS VERIFICATION | in-process deque; fine for single api container |

---

## 8. Backup / Recovery Review

| Item | Classification | Evidence |
|---|---|---|
| Runbook recovery actions (restart worker, whole stack, monitoring) | COMPLETE | `docs/runbook.md` §4 |
| DB backup script (`pg_dump`/restore), cron or Makefile target | MISSING | `scripts/` has only `smoke_phase1-3.sh`; plan §9 expected `backup_restore.md` |
| Restore procedure documented | MISSING | absent |
| Volume strategy (pgdata/redisdata/promdata/grafanadata) | COMPLETE | compose volumes; retention for prometheus TSDB 7d |

---

## 9. Secrets / Configuration Review

| Item | Classification | Evidence |
|---|---|---|
| `.env.example` committed, real `.env` gitignored | COMPLETE | `.gitignore:2-4` |
| Secrets in images? none baked | COMPLETE | Dockerfiles copy code only; env injected at runtime |
| Logs redact secrets | COMPLETE | `core/logging.py` |
| `.env.example` drift vs `Settings` | PARTIAL | `.env.example` still has legacy keys (`NEWS_API_KEY`, `FINNHUB_API_KEY`, `ECON_CALENDAR_SOURCE`, `RISK_*`, `OPENCODE_ZEN_MODEL`) vs actual `news_provider/…`; dev values only, everything `[REDACTED]` in audit |
| Prod SECRET_KEY override enforced | COMPLETE | `config.py:178-185` fail-closed (prod-up without real key refuses → NEEDS VERIFICATION live at prod-up) |
| Secrets rotation procedure | MISSING | no runbook section |
| Grafana default creds / anonymous viewer | NEEDS VERIFICATION | dev default `admin/admin` + pub viewer; decide prod posture |

---

## 10. SAFE MODE Verification

| Layer | Classification | Evidence / probe |
|---|---|---|
| L1 config: only `safe` accepted; invalid aborts startup | COMPLETE | `config.py:26-39,163-167`; `test_config_safe_mode.py` (26 tests) |
| L2 code: only `PaperBroker` implements broker; no live adapter module | COMPLETE | `app/broker/{paper,positions,costs}.py` only; grep no live broker |
| L3 orchestrator: hard mode-gate before publishing intent | COMPLETE | `app/decisions/engine.py`, `app/decisions/risk.py`; `test_decision_safety.py` (12 `safety` tests) |
| L4 startup/health: banner, `/health/live`, `/`, `/system/status`, `/health/ready` gate, UI badge | COMPLETE | `main.py:32-38`, `api/v1/system.py`; live: `{"status":"ok","mode":"safe"}` |
| L5 tests/CI: `safety` regression suite runs in CI | COMPLETE | `safety` marker on unit+integration tests; backend-ci runs unit suite |
| “No executor” enforcement (worker refuses executor role) | COMPLETE | `worker_main.py:62-64` raises SystemExit(2) |
| Live probe | COMPLETE | `TRADING_MODE`/`safe_mode` confirmed safe in 11-service live stack |

---

## 11. Missing or Incomplete Requirements

1. **Threat model / security checklist artifact** (req 1, 13) — MISSING.
2. **Automated dependency + image CVE scanning** (req 2, 12) — pip-audit, npm audit, trivy all MISSING in CI.
3. **CSP + security headers on the API & direct (dev) responses; header verification** (req 3) — PARTIAL.
4. **Proxy-correct client IP for rate limiting** (req 4) — PARTIAL.
5. **EXPLAIN / Redis-cache / WS-benchmark evidence** (req 5, 6, 7) — MISSING.
6. **TLS termination + HSTS/redirect; X-Forwarded-For + Secure-cookie under proxy; prod-overlay boot proof** (req 8, 9) — PARTIAL/MISSING.
7. **Backup/restore script + procedure** (req 10) — MISSING.
8. **Resource limits in prod overlay** (req 11) — MISSING.
9. **Secrets rotation runbook section** (req 14) — MISSING.
10. **Phase 10 hardening sign-off section/checklist** (req 16) — MISSING.
11. Minor: `frontend-ci.yml` does not run vitest; `make verify` omits backend integration + frontend build.

---

## 12. Recommended Changes

- **Docs/process first (zero runtime risk):** create `docs/security.md`/`threat-model.md` mapping plan §12 checklist → implementation + self-audit; add backup/restore runbook + secrets-rotation section; add Phase 10 sign-off checklist.
- **CI-only additions (no app behavior change):** add `pip-audit` + `npm audit` steps, a trivy image scan step in `docker-build.yml`, enable vitest in `frontend-ci.yml`.
- **Security headers (dev-safe):** add a FastAPI `SecurityHeadersMiddleware` (CSP, X-Frame-Options, nosniff, Referrer-Policy; HSTS only when prod) with tests; add matching Next headers — careful with Next dev/HMR under CSP.
- **Rate limiting:** honor `X-Forwarded-For` when behind a trusted proxy (config-gated) so per-IP limits survive nginx; keep dev behavior unchanged; document tuning baselines.
- **Perf evidence:** run + record EXPLAIN on `/market/candles`, signals, decisions, alerts; Redis usage review; a lightweight WS fan-out load check (e.g., 100 concurrent clients) — results into `docs/performance-review.md`. No app change required unless results demand it.
- **Prod hardening config:** add `deploy.resources.limits` in `docker-compose.prod.yml`; add nginx TLS block (listen 443 ssl, HSTS, 80→443 redirect) + cert provisioning notes/script (self-signed for dev proof).
- **Operations:** add `scripts/backup.sh` (pg_dump→`./backups`, gzip, retention) + `make backup`/`make restore`.
- **No changes to agents/orchestrator/backtest logic.** SAFE MODE layers untouched; `safety` tests stay green.

---

## 13. Files That Need Modification

- `.github/workflows/frontend-ci.yml` — add vitest to CI (small, correct gap).
- `.github/workflows/backend-ci.yml` — add `pip-audit` step (optional `safety`).
- `.github/workflows/docker-build.yml` — add trivy image scan step.
- `frontend/Dockerfile` / `backend/Dockerfile` — optional digest pinning.
- `docker-compose.prod.yml` — add `deploy.resources.limits`; expose 443; env overrides (SECRET_KEY, Grafana creds).
- `infra/nginx/nginx.conf` — add `listen 443 ssl`, cert paths, HSTS, 80→443 redirect; pass `X-Forwarded-For`/`Proto` (already proxied; backend must read them).
- `backend/app/api/deps.py` — trusted-proxy `client_ip` honoring `X-Forwarded-For` (config-gated).
- `backend/app/core/config.py` — optional new settings (`trusted_proxies`, header toggles, HSTS flag).
- `backend/app/main.py` — register `SecurityHeadersMiddleware` (conditional on app_env).
- `frontend/next.config.mjs` — security headers/CSP for Next responses.
- `scripts/smoke_phase1.sh` or new `scripts/phase10_smoke.sh` — probe TLS + headers + rate limit behind proxy once deployed.
- `Makefile` — add `backup`, `restore`, `audit-backend`, `audit-frontend`, `hardening-check` targets.

---

## 14. Files That Need Creation

- `docs/security.md` (or `docs/threat-model.md`) — plan §12 checklist → implementation mapping + self-audit/pen-checklist sign-off.
- `docs/performance-review.md` — EXPLAIN results, Redis cache review, WS fan-out benchmark numbers.
- `docs/backup-restore.md` — pg_dump/restore procedure, retention, restore drill.
- `scripts/backup.sh` (+ optional `scripts/restore.sh`, `scripts/make_self_signed_certs.sh`).
- `infra/nginx/certs/` (dev self-signed; prod: external) + placeholder README.
- Final: a Phase 10 hardening sign-off checklist section in `docs/runbook.md` (or the security doc).

---

## 15. Verification Commands

```bash
# stack up + healthy (post-audit baseline)
docker compose ps
curl -s http://localhost:8000/health/ready          # {"status":"ok","mode":"safe"}
curl -s http://localhost:8000/system/status | jq .components
curl -s http://localhost:9090/-/healthy             # prometheus
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000/

# local quality gates
make verify                                        # ruff, mypy, pytest, eslint, tsc
(cd frontend && npm test)                           # vitest 66
(cd frontend && npm run build)                      # next build

# after Phase 10 changes
(cd backend && .venv/bin/pip-audit)                 # 0 known advisories
docker run --rm aquasec/trivy fs image aur                         # per-image trivy
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
curl -s https://localhost:443/health/live -k | jq   # TLS + safe mode
curl -sI https://localhost/ | grep -iE 'strict-transport-security|content-security-policy'

# SAFE MODE stays enforced (must never change)
curl -s http://localhost:8000/health/live
docker compose exec api printenv TRADING_MODE      # safe
make test-backend-unit                             # safety tests incl.
```

---

## 16. Phase 10 Acceptance Checklist

- [ ] Threat-model/security checklist doc exists with plan §12 items mapped and signed off
- [ ] pip-audit + npm audit + trivy steps are green in CI; Dependabot active
- [ ] CSP + security headers verified on direct API, direct web, and via nginx; no dev regressions
- [ ] Rate limiting verified behind nginx (per-real-client-IP), tuned, documented
- [ ] EXPLAIN review, Redis review, WS fan-out benchmark recorded in `docs/performance-review.md`
- [ ] `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build` boots api+web+nginx healthy
- [ ] TLS on 443 with HSTS and 80→443 redirect; certs documented
- [ ] `make backup`/`make restore` work; restore drill performed once
- [ ] Resource limits set in prod overlay; no OOM observed in smoke
- [ ] Secrets rotation procedure documented; no secrets in logs/images/artifacts
- [ ] SAFE MODE verified end-to-end (L1–L5 + live probes) — unchanged and green
- [ ] Phase 10 hardening sign-off recorded in `docs/runbook.md`
- [ ] `make verify` green; `frontend-ci`/`backend-ci`/`docker-build` green in CI

---

## Numbered Implementation Sequence (safest / least disruptive first)

1. **Docs only (no code, no runtime impact)** — create `docs/security.md` (threat model + plan §12 mapping + self-audit checklist), `docs/backup-restore.md` with secrets-rotation section, extend `docs/runbook.md` with the Phase 10 sign-off checklist.
2. **CI-only additions** — enable vitest in `frontend-ci.yml`; add `pip-audit` step to `backend-ci.yml`; add `npm audit` step to `frontend-ci.yml`; add trivy scan step to `docker-build.yml`. All read-only checks, no app behavior change.
3. **Measurement / evidence pass (no app change)** — run EXPLAIN on `/market/candles`, signals, decisions, alerts; Redis usage review; basic WS fan-out load check; record everything in `docs/performance-review.md`. Only if findings warrant it, follow with targeted, reviewed changes.
4. **Security headers + CSP (dev-safe)** — add `SecurityHeadersMiddleware` to FastAPI (HSTS only in prod) with unit tests; add Next security headers; verify dev HMR unaffected; document.
5. **Proxy-correct rate limiting** — config-gated trusted-proxy `X-Forwarded-For` support in `api/deps.py`; update/extend rate-limit tests; keep dev defaults unchanged; document tuning baselines.
6. **Prod overlay resource limits** — add `deploy.resources.limits` to `docker-compose.prod.yml` (conservative values); no app change.
7. **Backup/restore tooling** — add `scripts/backup.sh`, `make backup`/`make restore`, run a restore drill; reference from runbook.
8. **TLS / NGINX production config** — add `listen 443 ssl` + HSTS + 80→443 redirect in `infra/nginx/nginx.conf`; self-signed dev certs script; then verify `prod-up` boots api+web+nginx healthy over TLS (the fail-closed SECRET_KEY gate also proves prod secret handling). Coordinate with step 5 for `X-Forwarded-For`.
9. **Final sign-off** — run the full verification suite (§15), confirm SAFE MODE probes, and record the Phase 10 hardening sign-off in `docs/runbook.md`.

Each step preserves SAFE MODE (L1–L5), touches no agent/orchestrator/backtest logic, removes no tests, and keeps all secrets out of logs/images/artifacts.