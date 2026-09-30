# Release Checklist — v0.1.0

Purpose: the operational gate list for the first development/staging release of the
Multi-Agent Forex AI System. Tick items explicitly; anything unchecked is a reason to
hold the release. Tag creation and publication are **FUTURE steps** and must not
happen without explicit operator approval.

Status: **complete (2026-09-30)** — Phase A completion closeout. All test/lint/build
gates verified green on the Phase A tree; `v0.1.0` tag exists at `d28af88`; tag
publication remains a **FUTURE step** requiring explicit operator approval.

---

## Tests

- [x] Backend unit: `make test-backend-unit` — 580 passed, coverage ≥ 90% (93.62%)
- [x] Backend integration: `make test-backend-integration` — 124 passed (real PG/Redis, 0 skipped)
- [x] Frontend: `npm test` (vitest) — 80 passed
- [x] (Final gate) full `make verify` on the committed tree — green (exit 0) 2026-09-30

## Lint & typecheck

- [x] `ruff check .` clean
- [x] `ruff format --check` clean (241 files)
- [x] `mypy app` clean (strict, 146 files)
- [x] `eslint .` clean
- [x] `tsc --noEmit` clean
- [x] `next build` clean

## Dependency security

- [x] pip-audit on runtime closure — 0 vulnerabilities
- [x] npm-audit policy gate — green (documented PostCSS exceptions only)
- [x] Trivy digest-pinned image scan — HIGH/CRITICAL exceptions only as documented
      (F-02 PostCSS, F-05 npm-CLI vendored)
- [x] Python lock files installed with `--require-hashes` in images, CI, and
      `make backend-venv`

## Docker builds

- [x] `make dev` — 11/11 compose services healthy
- [x] `make prod-up` overlay — api + web + nginx healthy behind HTTPS
- [x] Images run non-root; no host DB/Redis ports exposed; resource limits applied

## Production / staging health

- [x] `GET /health/live` → `{"status":"ok","mode":"safe"}`
- [x] `GET /health/ready` → `{"status":"ok","mode":"safe"}`
- [x] `GET /system/status` → db/redis/migrations OK, `version 0.1.0`, 5 workers `up`
- [x] Alembic migration head `0008` applied (`0008_phase13_paper_ledger`)
- [x] Frontend `http://localhost:3000` → HTTP 200
- [x] Grafana `http://localhost:3001` healthy; Prometheus `:9090` healthy

## HTTPS / WSS (staging)

- [x] HTTPS on 443: HTTP/2, TLS 1.2/1.3, 80→301 redirect
- [x] WSS to the API works (self-signed staging certs)
- [x] Real-CA TLS + HSTS: **BLOCKED on production domain** — documented, not a flaw

## SAFE MODE

- [x] Backend refuses non-`safe` `TRADING_MODE`
- [x] No live-broker module / executor worker present (`app/broker` = paper only)
- [x] Paper lifecycle operational (`PaperBroker`, migration `0008`,
      `test_paper_lifecycle_pipeline` green; 5 worker roles up)
- [x] M5/D1 not activated (market timeframes config-gated to M15/H1/H4)
- [x] `safety`-marked regression suite green in unit + CI
- [x] `/health/ready` and UI badge report SAFE MODE

## Backup / restore

- [x] `make backup` — dump + `.sha256` under `backups/` (gitignored)
- [x] Isolated restore drill passed 2026-09-12 (temp DB, live DB untouched)
- [x] Procedure documented (`docs/phase10-backup-restore.md`, `docs/runbook.md` §7)

## Secrets / git hygiene

- [x] `.env` not tracked; `.env.example` is the committed template
- [x] TLS key material not tracked (`infra/certs/*` ignored; `privkey.pem` mode 600)
- [x] No DB backups, caches, `node_modules`, `.next`, `.venv` tracked
- [x] `git ls-files` scan: no secrets/certs/backups/generated artifacts
- [x] Staged placeholder anchors: `infra/certs/.gitkeep`, `infra/certs/README.md`,
      `backups/.gitignore`, `backups/.gitkeep`
- [x] (Final gate) `git diff --check` — no whitespace errors (verified 2026-09-30)

## Documentation

- [x] `README.md` — status, quickstart, service addresses, prod/staging, security,
      backup/restore (refreshed 2026-09-12; no secrets)
- [x] `IMPLEMENTATION_PLAN.md` — status/progress reflects implemented Phases 0–10 + 12
- [x] `PROJECT_STATUS.md` — current verified status; deferred/blocked items explicit
- [x] `docs/architecture.md`, `docs/safe-mode.md`, `docs/runbook.md` — stale content fixed
- [x] `SECURITY.md` — reporting + posture
- [x] `docs/release-checklist.md` — this file

## Version consistency

- [x] Backend `0.1.0` (`pyproject.toml`, `app/core/constants.py`, `/system/status`)
- [x] Frontend `0.1.0` (`package.json`, `package-lock.json`)
- [x] No dependency versions changed during release prep

## CHANGELOG

- [x] `CHANGELOG.md` — `[0.1.0] - 2026-09-12` with Added / Security / Testing /
      Infrastructure / Documentation / Known Limitations; SAFE MODE statement; no
      invented features

## Final git review

- [x] `git status` — only intended files staged/modified (exactly the 5 closeout docs)
- [x] `git diff --stat` — reviewed change set
- [x] `git diff --check` — clean
- [x] `git ls-files` — hygiene scan re-run after staging (verified 2026-09-30)

## Tag creation

- [x] Annotated tag `v0.1.0` created at `d28af88` (`release: v0.1.0`)
- [x] `v0.1.0` matches CHANGELOG `[0.1.0]` and backend/frontend versions (`0.1.0`)

## Release publication — FUTURE (requires explicit operator approval)

- [ ] Push the commit + tag to the remote
- [ ] (Optional) Draft release notes from `CHANGELOG.md` `[0.1.0]`