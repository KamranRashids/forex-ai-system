# Phase 11 — Release Audit

Audit date: 2026-09-12 (UTC). Scope: **read-only** audit of the current repository to
determine exactly what is required to produce the first clean `v0.1.0`
development/staging release — no application features added, nothing fixed, nothing
committed, no tag created.

---

## 1. Purpose

Determine the current release state and a precise, classified gap list so the operator
can cut the first release cleanly. This document is the audit record; it changes nothing.

---

## 2. Current Git State

| Item | Value |
|---|---|
| Branch | `main` |
| HEAD commit | `a6cddb78b3698f8291d4a3f8b1100d78038b3bf2` — `a6cddb7 feat(frontend): candle source on signals page + status text fixes` (2026-09-01, KamranRashids) |
| Total commits | 19 (conventional `feat`/`fix`/`chore`, phase-labeled — clean source for release notes) |
| Git tags | **none** (no tag of any kind; `v0.1.0` will be the first) |
| Working tree clean? | **No** — 41 changes (17 modified tracked files + 24 untracked files) |
| Uncommitted changes? | Yes — all of them are the intentional, verified Phase 10 / audit artifacts (CI hardening, Dockerfiles, `config.py`, `api/deps.py`, prod overlay, nginx, runbook, `docs/phase10-*.md`, `threat-model.md`, locks, `scripts/backup_db.sh`, `restore_db.sh`, `.trivyignore`, `test_client_ip.py`, `PHASE_10_AUDIT.md`, `PROJECT_STATUS.md`) plus this audit doc |
| Generated/runtime files tracked? | **No** — `git ls-files` (278 files) contains no `node_modules/`, `__pycache__/`, `.next/`, `out/`, `.venv/`, `.pgdata/`, `.redisdata/`, caches, `*.sql.gz`, `*.log`, `.tsbuildinfo`, `dist/`, `build/` |
| Untracked placeholders inside ignored dirs | `infra/certs/.gitkeep` + `README.md`, `backups/.gitignore` + `.gitkeep` (untracked; need `git add` at release time — data files inside both dirs stay ignored) |

**Key consequence:** the audited hardening, docs, CI gates, and lock files exist only in
the working tree. A fresh clone from the remote today would receive **none of them** —
this is the single biggest release blocker (see §8, §10).

---

## 3. Current Version State

| Reference | Location | Value |
|---|---|---|
| Project/package version (backend) | `backend/pyproject.toml:7` | `0.1.0` |
| Runtime APP_VERSION | `backend/app/core/constants.py:6` | `0.1.0` |
| Served by API | `backend/app/api/v1/system.py:54,206`; `backend/app/main.py:37,57` (OpenAPI) | `0.1.0` — confirmed live: `/system/status` → `version: 0.1.0` |
| Package version (frontend) | `frontend/package.json:3` | **`0.0.1`** |
| Lockfile root package version | `frontend/package-lock.json:3,9` | **`0.0.1`** |
| Makefile / compose / Dockerfiles / nginx | — | no version references; images are compose-named, unversioned |
| Docs referencing the target | `IMPLEMENTATION_PLAN.md:699`, `PROJECT_STATUS.md:42,73,115,126` | `v0.1.0` |

**Must be updated before `v0.1.0`:** `frontend/package.json` and `frontend/package-lock.json`
→ `0.1.0` (back-end already consistent at `0.1.0`; frontend is the only drift).
**Load-bearing consistency check:** `package-lock.json` root version must match
`package.json` (update both together, e.g. `npm version 0.1.0 --no-git-tag-version`).
No other code carries a version that must change.

---

## 4. Release Requirements

Presence + classification. “Required” = needed for a clean *first* release; items are
not assumed — single-operator, pre-production system.

| Requirement | Exists? | Classification | Notes |
|---|---|---|---|
| CHANGELOG.md | **No** | **REQUIRED** | Plan §14 Phase 11 mandates it; seed from the 19 conventional commits (log already phase-labeled) |
| Release notes | **No** | **REQUIRED** (covered by CHANGELOG) | No separate release-notes doc needed at v0.1.0 |
| Release checklist | **No** | **RECOMMENDED** | A short `docs/releasing.md` or CHANGELOG appendix; plan also suggests branch-protection notes — advisory for a solo repo |
| Issue template | **No** | OPTIONAL | No external contributors; add when the repo goes public |
| Bug report template | **No** | OPTIONAL | Same; bundle with issue template work later |
| Feature request template | **No** | NOT APPLICABLE for v0.1.0 | Roadmap is plan-driven; revisit when contributors appear |
| Pull request template | **No** | OPTIONAL | Solo operator workflow — value is low today |
| Contribution guidance (CONTRIBUTING.md) | **No** | OPTIONAL | `.pre-commit-config.yaml` + README “development workflow” already cover the essential contract |
| Security reporting guidance (SECURITY.md) | **No** | **RECOMMENDED** | MIT public repo; a short `SECURITY.md` (reporting contact + “NO live trading exists / do not request real-money features”) is cheap and aligned with §9 |
| Reproducible setup instructions | **Yes** | **REQUIRED — satisfied** | README quickstart; `make backend-venv` from hashed `requirements-dev.lock`; `.env.example` template |
| Docker startup instructions | **Yes** | **REQUIRED — satisfied** | README §Quickstart (`cp .env.example .env` → `make dev`); `make prod-up`; compose has safe `${VAR:-default}` fallbacks for every variable |
| SAFE MODE instructions | **Yes** | **REQUIRED — satisfied** | README top banner + `docs/safe-mode.md` (5 enforcement layers, verification, kill switch, escalation policy) |
| Testing instructions | **Yes** | **REQUIRED — satisfied** | README development workflow + Makefile (`verify`, `test-backend-unit`, `test-backend-integration`, frontend targets). Note: `make verify` omits vitest / next build / integration — see §11 |
| Backup/restore instructions | **Yes** | **REQUIRED — satisfied** | `docs/phase10-backup-restore.md` + `docs/runbook.md` §7; live drill passed 2026-09-12 |
| Environment configuration documentation | **Yes** | **REQUIRED — satisfied** | `.env.example` is fully commented (core/market/news/LLM/frontend); matches compose + `Settings` |
| Current architecture documentation | **Yes, stale header** | **REQUIRED — present, needs refresh** | `docs/architecture.md` exists (5 ADRs tracked); header still “scaffold (Phase 0)” and a few rows read as future tense (§5) |

---

## 5. Documentation Audit

Stale or contradictory statements vs the actual implementation (reported, not fixed):

| Doc | Stale claim | Reality (verified 2026-09-12) |
|---|---|---|
| `IMPLEMENTATION_PLAN.md:3-4` | “**PROPOSED — awaiting approval** … currently **empty**” | 19 commits; Phases 0–10 + 12 implemented; Phase 10 accepted |
| `IMPLEMENTATION_PLAN.md:750` | “**No application code has been written.**” | Direct contradiction of the 19-commit history |
| `PROJECT_STATUS.md:3` | “clean working tree” | Working tree has 41 changes |
| `PROJECT_STATUS.md:12,15,16` | `web` never started; Prometheus stopped; Grafana empty | Live stack: all 11 services **healthy**, web HTTP 200 (probed today) |
| `PROJECT_STATUS.md:25,26` | Prod overlay without TLS; no trivy/audit in CI | Staging TLS live on 443 (HTTP/2, WSS); Trivy digest-pinned scan + npm-audit + pip-audit gates in CI |
| `PROJECT_STATUS.md:44` | “pytest 554” framing | Current suites: unit 463 @ 92.61% (≥90), integration 102 |
| `PROJECT_STATUS.md:89` | `.env.example` still contains legacy keys (`NEWS_API_KEY`, `FINNHUB_API_KEY`, `ECON_CALENDAR_SOURCE`) | `.env.example` is already modernized (`NEWS_PROVIDER`, `CALENDAR_PROVIDER`, `FINNHUB_API_TOKEN`); **only the gitignored local `.env`** retains legacy key names — not a release artifact |
| `README.md:84-91` | Status table: “Phase 4 next”, “5–11 pending” | Actual: Phases 0–10 (+12) done; Phase 11 = this release |
| `docs/architecture.md:3` | “Status: scaffold (Phase 0)” | System is implemented and Phase 10 signed off |
| `docs/safe-mode.md:12,15,16,18` | Status column “planned Phase 5” / “partial (growing per phase)” | L1–L5 all active and verified |
| `docs/runbook.md:6` + §1 | Scoped “Phase 7”; stack table omits `web` + `worker-alerts`; Grafana at `:3000` | Grafana is on **`:3001`** in dev (`docker-compose.yml:319`); web owns `:3000`; runbook now also covers Phase 10 backup/restore (§7) |

Accurate (unchanged) statements worth keeping: PROJECT_STATUS §3 frontend-scope note
(no charts/portfolio/paper-executor pages — confirmed, `frontend/src/app` has
login/alerts/signals/backtests/home only); MIT license in `README.md`/`LICENSE`.

---

## 6. Repository Hygiene

- 278 tracked files; **zero** generated/runtime artifacts tracked (see §2 scan).
- `.gitignore` correctly covers: `.env`, `.env.*` (except `.env.example`), Python caches, `.venv/`, `node_modules/`, `.next/`, `out/`, `next-env.d.ts`, `*.tsbuildinfo`, `.eslintcache`, `.pgdata/`, `.redisdata/`, `*.sql.gz` (so DB dumps in `backups/`), `*.log`, `infra/certs/*` (except `.gitkeep` + `README.md`), IDE files.
- On-disk caches (`.mypy_cache`, pytest/ruff caches, `node_modules`, `backend/.venv`) exist but are all ignored; `make clean` removes build caches.
- `infra/certs/.gitkeep` + `README.md` and `backups/.gitignore` + `.gitkeep` are untracked placeholders — data files inside remain ignored, so `git add` of the placeholders is safe and recommended before release.
- `LICENSE` (MIT, Copyright 2026), `.editorconfig`, `.pre-commit-config.yaml`, `.github/dependabot.yml` all tracked.

## 7. Security Release Check

Release contents would **not** include secrets/keys/backups/runtime data — verified:

| Category | Status |
|---|---|
| `.env` credentials | Not tracked (`git ls-files` has no `.env`; `git check-ignore` confirms). Only `.env.example` w/ explicit `change-me` placeholders is committed |
| TLS private keys / certs | `infra/certs/*` ignored; `privkey.pem` is mode `600` on disk; only `.gitkeep` + README are trackable |
| Database backups | `*.sql.gz` ignored; `backups/` holds only placeholders |
| API keys / tokens / credentials | None in tracked files — Phase 10 final audit scanned `git diff` for key/token/`BEGIN PRIVATE KEY` patterns (no matches); service env is injected at runtime via `.env` |
| Generated runtime data | None tracked (caches, `.next`, node_modules, pgdata/redis/prometheus/grafana volumes all excluded or untracked) |
| Dev-only posture to document in release notes | Grafana dev creds `admin/admin` + anonymous Viewer and unauthenticated `/metrics` are dev-stack defaults (`docker-compose.yml:309-317`); operator must override env vars before any wider deployment |

## 8. Fresh-Clone Readiness

Audited against the intended flow: `git clone` → `cp .env.example .env` → `make dev`.

1. **Clone** — repo is self-contained (no submodules, no LFS).
2. **Configure env** — `.env.example` is current and matches compose + `Settings`; every
   compose variable has a `${VAR:-default}` fallback, so the stack boots even with zero
   overrides. Requires: WSL2/Docker + Compose v2, GNU make, Python 3.12 (host venv),
   Node ≥ 22 (frontend dev) — documented in README.
3. **Start dev stack** — `make dev` builds postgres/redis/api/5 workers/web (+
   prometheus/grafana on `make dev`; all in `docker-compose.yml`). Healthchecks gate
   `depends_on`; migrations run on worker/API boot (`alembic upgrade head`).
4. **Access frontend** — `http://localhost:3000` → **HTTP 200** (probed 2026-09-12).
5. **Access API** — `http://localhost:8000/docs`, `/health/live` → **200**.
6. **Verify health/readiness** — `/health/live` + `/health/ready` =
   `{"status":"ok","mode":"safe"}` (probed).
7. **Verify system status** — `/system/status` green; `version 0.1.0`; 11/11 containers
   healthy (probed).
8. **Run tests** — `make backend-venv` (hashed dev lock) then `make verify`;
   `make test-backend-integration` needs the compose PG/Redis; `frontend: npm ci` +
   `npm test` (vitest 66/66) — all verified on this tree.
9. **SAFE MODE awareness** — README banner + persistent UI badge + `docs/safe-mode.md`.

**Anything that would prevent a clean fresh-clone setup today?**
- **The entire Phase 10 + Phase 11 working-tree content is uncommitted.** A clone from
  the remote HEAD (`a6cddb7`, 2026-09-01) would lack: CI hardening (pip-audit/npm-audit/
  Trivy gates), `requirements{,-dev}.lock`, hardened Dockerfiles, trusted-proxy rate
  limiting, prod overlay limits + staging TLS, backup/restore scripts, all
  `docs/phase10-*.md`, `threat-model.md`, audit docs, and Phase 10 final sign-off. So
  step 1+ works but reproduces a pre-hardening system. **This is a release blocker.**
- Stale docs (§5) would mislead a fresh user about phase status, version, and Grafana port.
- Minor: local (gitignored) `.env` has legacy key names vs `.env.example`; irrelevant to
  a fresh clone but confusing if that file is ever shared.
- Minor: `make verify` doesn’t run vitest / next build / integration (§11) — gate gap, not a blocker.

## 9. SAFE MODE Release Safety

Structural, verified, and preserved by this release:

- **L1 Config** — `TRADING_MODE` accepts only `safe` (`config.py` `frozenset({"safe"})`); any other value halts startup; `SECRET_KEY` min length 32 enforced.
- **L2 Code** — only `PaperBroker` exists; no live-broker module anywhere (`app/broker/`).
- **L3 Orchestrator** — decision pipeline hard-gates mode before publishing intents.
- **L4 Runtime** — boot banner; `/health/live`, `/`, `/system/status` expose the mode; `/health/ready` only returns ok when safe; UI shows a persistent badge.
- **L5 Tests/CI** — `safety`-marked regression suite runs in CI (26 config + decision-safety tests green).
- Worker refuses executor role (`worker_main.py` raises `SystemExit(2)`).
- Live probe 2026-09-12: `printenv TRADING_MODE` = `safe`; `/health/ready` = `{"status":"ok","mode":"safe"}`.

Release requirement: the v0.1.0 tag and its checks must re-run the §9 probes; SAFE MODE
contract text in `README.md` + `docs/safe-mode.md` must stay intact.

## 10. Required Release Changes

Required to produce a *clean* `v0.1.0` (nothing here adds a feature; all are release-prep):

1. **Commit the current tree** (Phase 10 artifacts + audits) so a fresh clone actually
   contains the hardened, scanned, documented system that was audited. *Precondition for
   everything else; without it the tag misrepresents the release content.*
2. **Create `CHANGELOG.md`** covering `0.1.0`, seeded from the 19 conventional commits
   (history is already phase-labeled: 0 repo init → 12 observability/data-source truth).
3. **Sync frontend version to `0.1.0`** in `frontend/package.json` and
   `frontend/package-lock.json` (backend already `0.1.0`; single coherent version for the tag).
4. **Refresh stale documentation** (§5): `IMPLEMENTATION_PLAN.md` header/approval status
   (remove “PROPOSED/empty” + “no application code has been written”), `PROJECT_STATUS.md`
   status claims (web/prometheus/TLS/trivy/tests/.env.example), `README.md` status table,
   `docs/architecture.md` header, `docs/safe-mode.md` status column, `docs/runbook.md`
   Grafana port (`:3001`) + stack table.
5. **Stage the placeholder files** (`infra/certs/.gitkeep`+`README.md`,
   `backups/.gitignore`+`.gitkeep`) so ignored dirs have tracked anchors.

## 11. Recommended Release Changes

Not blockers, but advisable for a clean, reviewable first release:

- `SECURITY.md` — reporting guidance + the SAFE MODE / no-live-trading note.
- Issue templates (bug + feature) and a PR template — add now or when the repo goes public.
- `CONTRIBUTING.md` — codify the README dev workflow + pre-commit contract.
- A release checklist note (single-page `docs/releasing.md`): `make verify` → CI green → tag `v0.1.0` → re-probe §9.
- Extend `make verify` to include `vitest`, `next build`, and the integration suite (currently omitted).
- Decide tag shape now: annotated `git tag -a v0.1.0` (suggested) vs lightweight.
- (Plan Phase 11 also lists a demo/replay script for offline demo data — OPTIONAL for v0.1.0, the synthetic provider already runs fully offline.)

## 12. Release Checklist

Proposed (for the operator; **not executed** by this audit):

- [ ] Backend unit 463 @ ≥90% and integration 102 green; ruff/mypy/eslint/tsc/vitest 66/66 green
- [ ] CI green (backend-ci, frontend-ci, docker-build incl. Trivy scan), Dependabot active
- [ ] Fresh-clone proof: clone → `cp .env.example .env` → `make dev` → web :3000 + api :8000 + health reads `safe`
- [ ] `/health/live` + `/health/ready` = `{"status":"ok","mode":"safe"}`; `/system/status` shows `version 0.1.0`
- [ ] `make backup` + isolated restore drill recorded (2026-09-12 drill already passed)
- [ ] No `.env`/certs/backups/secrets tracked; placeholders staged (§10.5)
- [ ] `CHANGELOG.md` = commits → 0.1.0; frontend version synced to 0.1.0
- [ ] Stale docs (§5) refreshed; README quickstart still ≤5 commands
- [ ] Tag `v0.1.0` (annotated) created **only** upon explicit operator approval

## 13. v0.1.0 Readiness

| Dimension | Status |
|---|---|
| Code, tests, CI, hardening, docs evidence | Ready (verified working tree) |
| Release artifacts (CHANGELOG, version sync, refreshed status docs) | **Not present** |
| Commit state (everything on disk is uncommitted) | **Not releaseable as-is** |
| Fresh-clone equivalence (remote HEAD vs audited tree) | **Not equivalent** |
| Tags | None — first tag will be `v0.1.0` |

The audited *content* is release-quality; the *release state* is not yet cut.
Given uncommitted work is the precondition and requires an explicit human decision to
commit/tag (outside this audit’s remit), v0.1.0 is **not yet ready to tag**, pending
§10 items 1–4.

## 14. Final Assessment

The system itself — runtime, tests, CI gates, security hardening, backup/restore, SAFE
MODE verification — is release-quality and was re-verified live on 2026-09-12. The
release is blocked by **release-state** gaps, not code defects: the entire hardened
tree is uncommitted (so a fresh clone would not contain it), no CHANGELOG exists, the
frontend version is out of sync (`0.0.1` vs `0.1.0`), and the status/architecture
documents still describe a pre-implementation or pre-hardening state.

**Exact blockers for a clean `v0.1.0`:**
1. Commit the full working tree (Phase 10 + audits) — fresh clone must equal the audited system.
2. Create `CHANGELOG.md` (seed from the 19 commits; version `0.1.0`).
3. Sync `frontend/package.json` + `package-lock.json` to `0.1.0`.
4. Refresh stale docs: `IMPLEMENTATION_PLAN.md` header, `PROJECT_STATUS.md`, `README.md` status table, `docs/architecture.md`, `docs/safe-mode.md`, `docs/runbook.md` (Grafana port/stack table).

**RELEASE NOT READY**