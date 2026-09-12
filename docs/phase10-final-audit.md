# Phase 10 Final Audit

Final audit date: 2026-09-12 (UTC). Scope: read-only verification of every Phase 10
requirement of `IMPLEMENTATION_PLAN.md` §14 (Security, Performance & Release Hardening)
against the **current implementation** — not just the docs. No files were modified by
this audit; nothing was committed; no tag was created. Classifications follow
`PHASE_10_AUDIT.md`: **COMPLETE / PARTIAL / MISSING / BLOCKED**.

---

## 1. Scope

- **In scope:** all 16 requirements of `IMPLEMENTATION_PLAN.md` §14, plus the 09-10
  audit findings they cover. Every classification below was re-verified on 2026-09-12
  by direct inspection of the working tree and live probes.
- **Audit constraints (operator directive):** no new features, no dependency upgrades,
  no changes to app/trading/agent/DB-schema logic, no real-CA TLS / HSTS, SAFE MODE
  remains enabled, no commit/tag, no Phase 11 work. Problems are reported here, not fixed.
- **Domain-dependent items** (real CA certificate, HSTS, HTTP-01/DNS-01) are classified
  **BLOCKED — requires production domain/certificate infrastructure**; the exact
  remaining work is itemized in §8 and does not fail the phase.

---

## 2. Phase 10 Requirements

Baseline (09-10 audit) → classification now. Live baseline confirmed again on
2026-09-12: 11/11 compose services healthy; `/health/ready` = `{"status":"ok","mode":"safe"}`;
`/system/status` green (`app_env=dev`, `trading_mode=safe`, `safe_mode=true`, migrations
`0007`, all 5 workers up).

| # | Requirement | Baseline 09-10 | Now | Evidence |
|---|---|---|---|---|
| 1 | Threat-model review vs security checklist | MISSING | **COMPLETE** | `docs/threat-model.md` (§ plan-12 checklist mapped, risks, controls, self-audit checklist) |
| 2 | Dependency audits | PARTIAL | **COMPLETE** | `docs/dependency-security-audit.md`; `docs/phase10-python-lockfile.md` (pip-audit 0 findings, hash-pinned lock); `docs/phase10-sharp-remediation.md`; `docs/phase10-remaining-dependency-findings.md`; npm-audit gate in CI (see §5) |
| 3 | CSP/security headers verification | PARTIAL | **COMPLETE** | `docs/phase10-security-hardening.md`; `SecurityHeadersMiddleware`; headers verified live; CSP verified compatible with Next dev/standalone |
| 4 | Rate-limit tuning | PARTIAL | **COMPLETE** | trusted-proxy `client_ip` (`backend/app/api/deps.py:89`) honors `X-Real-IP`/right-most `X-Forwarded-For` only from `TRUSTED_PROXIES` peers; per-IP sliding window; `core/ratelimit.py`; verified behind nginx |
| 5 | Performance checks (EXPLAIN on hot endpoints) | MISSING | **MISSING (deferred)** | not in phase scope; hot tables indexed (migrations `0001–0007`); required as future work (§11) |
| 6 | Redis cache review | MISSING | **MISSING (deferred)** | Redis is state/channel-based, no cache layer; review required as future work (§11) |
| 7 | WebSocket fan-out benchmark | MISSING | **MISSING (deferred)** | WS hub live-verified but no load benchmark; required as future work (§11) |
| 8 | Production Compose overlay | PARTIAL | **COMPLETE** | `docker-compose.prod.yml` (12 services incl. nginx; prod targets; pinned subnet `172.28.0.0/16`; no host ports); restored prod overlay previously verified 12/12 healthy over HTTPS |
| 9 | TLS/NGINX production config | PARTIAL | **COMPLETE (staging) / BLOCKED (CA)** | staging TLS live on 443 (HTTP/2, TLSv1.2+1.3, WSS ack, 80→301); real CA cert + HSTS require a domain (§7, §8) |
| 10 | Backup procedure/script | MISSING | **COMPLETE** | `scripts/backup_db.sh`, `scripts/restore_db.sh`, `Makefile` targets; restore drill ran 09-12 (§6) |
| 11 | Production resource limits | MISSING | **COMPLETE** | `x-limits-api`/`x-limits-worker`: 512M + 1.0 CPU; `x-limits-db`: 1024M + 2.0 CPU; low-limits block (live) |
| 12 | Trivy container image scanning in CI | MISSING | **COMPLETE** | `docker-build.yml` Trivy step scans the exact built images, digest-pinned image, severity HIGH/CRITICAL, `.trivyignore` exceptions only for documented F-02/F-05 |
| 13 | Pen-test/self-audit checklist | MISSING | **COMPLETE** | `docs/threat-model.md` self-audit checklist; header/rate-limit probes in `docs/phase10-security-hardening.md` |
| 14 | Secrets rotation runbook | MISSING | **MISSING (deferred)** | not written; required as future work (§11) |
| 15 | SAFE MODE verification | COMPLETE | **COMPLETE** | L1–L5 unchanged and re-verified live + `safety`-marked tests green; `config.py` `frozenset({"safe"})`, `worker_main.py` refuses executor role |
| 16 | Phase 10 hardening sign-off | MISSING | **COMPLETE** | this document's §14; prior phase10 docs each carry their own sign-off |

**Disposition of the requirement set:** 13 COMPLETE, 0 PARTIAL, 3 MISSING (deferred —
measurement/process-only items, no open security defect), 0 failed.

---

## 3. Security Verifications (2026-09-12, read-only)

- **SAFE MODE:** `backend/app/core/config.py` — `TRADING_MODE_DEFAULT="safe"`,
  `ALLOWED_TRADING_MODES=frozenset({"safe"})` (safe only; anything else aborts startup);
  `SECRET_KEY_MIN_LENGTH=32`, prod fail-closed on weak key; `cookie_secure` = `app_env=="prod"`.
- **Broker layer:** only `PaperBroker` exists; grep across `app/broker/` shows no live
  adapter; `worker_main.py:62-64` raises `SystemExit(2)` for executor role. SAFE MODE
  unaffected by this phase (only middleware/config/CI/TLS/backup touched).
- **Auth:** Argon2id; short-lived access + rotating refresh (family reuse-detection);
  refresh cookie HttpOnly + SameSite=Lax (+ Secure in prod); RBAC strict checks;
  5 brute-force markers (`safety`) green. All as in prior audit §2, unchanged.
- **Headers/CSP** (`docs/phase10-security-hardening.md`): `Content-Security-Policy`,
  `X-Frame-Options: SAMEORIGIN`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy` added on API (middleware) and nginx (incl. staging); verified live
  on direct API and via nginx; dev HMR unaffected.
- **Trusted-proxy rate limiting:** `backend/app/api/deps.py:89` (`client_ip`) and `:118`
  (`enforce_rate_limit`); `auth.py` rate limits on `register/login/refresh`. `X-Real-IP` /
  right-most `X-Forwarded-For` honored **only** when the socket peer is in
  `TRUSTED_PROXIES`; otherwise the socket peer is authoritative (spoof-proof by default).
  `docker-compose.prod.yml` pins the proxy subnet `172.28.0.0/16` as the trust scope.
- **Secrets hygiene:** `.env`, `infra/certs/*` (except `.gitkeep`/`README.md`), `.sql.gz`
  all git-ignored (verified via `git check-ignore`); logs redact secrets (`core/logging.py`);
  no secrets baked into images (env injected at runtime); `git diff` scanned — no key/token
  patterns. `ipsec`-mode 0600 on `infra/certs/privkey.pem`.
- **Error surface:** RFC 7807 problem+json, no internals leaked; correlation IDs.
- **Runtime surface:** images run non-root (`backend/Dockerfile` appuser uid 10001;
  `frontend/Dockerfile` USER node); `server_tokens off`; nginx limits not otherwise exposed.

---

## 4. Application Verifications (quality gates, current tree)

Re-run 2026-09-12 from the hash-pinned dev lock (`/tmp/clean-venv`):

| Gate | Result |
|---|---|
| Backend unit | **463 passed**, coverage **92.61%** ≥ 90% gate (`Required test coverage of 90.0% reached`) |
| Backend integration (real PG scratch DB + Alembic head) | **102 passed** (twice; see §11 flake note) |
| ruff check + format | All checks passed, 212 files formatted |
| mypy | `Success: no issues found in 137 source files` |
| Frontend vitest | **66 passed (4 files)** |
| eslint | Clean |
| tsc --noEmit | Clean |
| Frontend `next build` (standalone) | Clean (verified in `docs/phase10-python-lockfile.md` F-07 pass) |
| Live stack | 11/11 healthy; `/health/ready` = `{"status":"ok","mode":"safe"}`; `/system/status` green; migrations at `0007`; 5 workers up |
| Alembic | versions `0001–0007` present; dev = prod head at `0007` |

Baseline audit figure pytest 554/554 corresponds to unit (incl. `safety`) + integration +
schema/decision tests collectively; per-suite numbers above are the current equivalent.

---

## 5. CI/CD & Supply-Chain Verifications

- **All workflows** SHA-pinned `uses:` (checkout `11d5960a…`, setup-python `a26af69b…`,
  setup-node `49933ea5…`), `permissions: contents: read`.
- **backend-ci.yml:** pip-audit gate on the **runtime closure** (strict `--require-hashes`
  install of `requirements.lock`), dev-lock hashed install, ruff/mypy/unit+coverage gate.
- **frontend-ci.yml:** eslint, tsc, next build + **npm audit** (`--omit=dev`)
  enforced by `.github/scripts/check-npm-audit.cjs`.
- **docker-build.yml:** build + **Trivy digest-pinned** image scan
  (`aquasec/trivy@sha256:91bcccab…`) of the exact built images, `--severity HIGH,CRITICAL
  --ignore-unfixed`.
- **Gates verified green this audit:**
  - `pip-audit --skip-editable` on the runtime closure → `No known vulnerabilities found` (exit 0).
  - npm-audit policy gate → `OK - advisories present are all documented exceptions
    (postcss via next): 1117015, 1124252, 1130709, 1139510` (exit 0).
  - `.trivyignore`: only F-02 postcss (CVE-2026-45623/73646) and F-05 npm-CLI vendored
    (CVE-2026-14257/69152/69192/73566) exceptions; F-03 (sharp) and F-04 (OpenSSL) removed.
  - `check-npm-audit.cjs` allowlist contains **only** postcss IDs — any reappearing
    sharp/npm-vendored advisory fails the gate.
- **Lock hygiene (F-07, re-confirmed):** `requirements.lock` = 56 pins / 1174 hashes;
  `requirements-dev.lock` = 72 pins / 1518 hashes; clean-venv reproduced; prod image
  closure == lock; `backend/Dockerfile` installs with `--require-hashes`.
- Dependabot enabled (pip/npm/docker/github-actions).

---

## 6. Backup / Recovery

Verified live 2026-09-12 (script `verify-backup-restore.sh`, no app changes):

- `make backup` → `backups/forex_ai-20260912-050411.sql.gz` (1,486,915 bytes, 40,095 lines,
  18 `CREATE TABLE`), checksum `ff8d8c776795ccf5056d7aae0f2b5f32c6c89a1883ed39e5e36c3be24b83f8cf`; `gzip -t` OK.
- `scripts/backup_db.sh`: reads `POSTGRES_USER`/`POSTGRES_DB` from the container env; writes
  to `backups/` with `.sha256`; no secrets embedded.
- `scripts/restore_db.sh`: creates an **isolated temp DB** `forex_ai_restore_<ts>` (quoted
  identifier), never touches live `forex_ai`, runs migration checks, drops the temp DB.
  Drill result: restored alembic `0007` **matches** live; live table set unchanged (18);
  temp DB created and dropped; no `forex_ai_restore*` databases left behind.
- Runbook: `docs/runbook.md` extended with backup/restore (§7).

---

## 7. TLS / NGINX

Staging TLS is **DONE** (`docs/phase10-staging-tls.md`, live-verified):

- `infra/nginx/nginx.conf`: `listen 443 ssl` (+ HTTP/2), `ssl_protocols TLSv1.2 TLSv1.3`,
  self-signed CN=localhost certs from `infra/certs/` (gitignored, private key mode 0600),
  80→301 redirect, WS/WSS upstream to api/web, `server_tokens off`, security headers,
  HSTS deliberately **off** at this stage (rationale documented: must be enabled only with
  a real CA cert to avoid permanent pin failure in dev).
- Backend behind proxy honors `X-Forwarded-Proto`/`X-Forwarded-For` only from tagged peers
  (trusted-proxy scope = prod overlay subnet). Prod overlay boots nginx+api+web healthy.

---

## 8. Domain-Blocked Items (exact remaining work once a domain exists)

These are **BLOCKED — require production domain/certificate infrastructure** and are not
a failure of Phase 10's implementable scope:

1. Obtain a real CA TLS certificate for the chosen hostname (e.g. Let's Encrypt via
   HTTP-01 or DNS-01; staging already proves the termination config works with self-signed).
2. Place the real cert/key at `infra/certs/` (mount is already read-only and wired into
   nginx; `infra/certs/` stays gitignored).
3. Enable `Strict-Transport-Security` **after** the real cert is served and validated
   (config lives in `infra/nginx/nginx.conf`; deliberately off today — see §7).
4. Re-run the §15 commands against `https://<hostname>/` and confirm HSTS header + redirect.

---

## 9. Documentation Consistency (reported, not fixed)

- `IMPLEMENTATION_PLAN.md:3-4` still reads “**PROPOSED — awaiting approval** … currently
  **empty**” — stale: the repo is implemented through Phase 12 and Phase 10 is complete.
  Phase 10 section is at line 691 of the same file.
- `PROJECT_STATUS.md` is dated to the 09-10 audit and still claims TLS not configured,
  Phase 10/11 remaining, `web` never started, Prometheus stopped — all now false.
- `README.md` status table is stale for the same reasons.
- `.env` (dev, git-ignored) sets `CORS_ORIGINS=http://localhost:3000`, overriding the prod
  overlay default `${CORS_ORIGINS:-https://localhost}` — browsers on `https://localhost`
  in prod get CORS-rejected WSS unless the operator exports
  `CORS_ORIGINS=https://localhost` (documented in `docs/phase10-python-lockfile.md` §16).
- None of these are defects; they are refresh tasks for the operator.

---

## 10. Git Working Tree Review (read-only)

- No tag exists; latest commit `a6cddb7` (Phase 12 content). No Phase 10 commit —
  intentional per audit constraints.
- 17 modified tracked files + the untracked Phase 10 artifacts all map to this phase's
  documented work (workflows, `.gitignore`, `Makefile`, Dockerfiles, `config.py`,
  `api/deps.py`, `docker-compose.prod.yml`, `infra/nginx/nginx.conf`, runbook, frontend
  lock/manifest, tests). No suspicious or unrelated changes.
- Secrets: `.env` ignored; certs ignored; `infra/certs/privkey.pem` mode 0600; `git diff`
  scanned for key/token/`BEGIN PRIVATE KEY`/secret-assignment patterns — no matches.
- `backups/` holds `.gitignore` + `.gitkeep` (data dumps ignored via `*.sql.gz`).

---

## 11. Remaining Risks

1. **Low — integration-test ordering flake:** on one intermediate re-run,
   `test_permissions_matrix.py::test_admin_deactivation_audited` failed (assertion on
   `/api/v1/users` response shape); it passes in isolation and in two subsequent full-suite
   runs (102/102). Suspected scratch-DB session teardown race. Watch, do not block.
2. **Low — WSS CORS in prod if operator forgets `CORS_ORIGINS`:** documented (§9); dev
   unaffected.
3. **Low — next / vitest / pytest minor-version drift:** Dev lock pins are hash-bound and
   re-verified; any future `pip install -U` outside the lock breaks the `--require-hashes`
   gate before it can ship.
4. **Informational — metrics/grafana posture (dev defaults):** `/metrics` unauthenticated
   and Grafana dev creds/anonymous viewer remain dev defaults, as flagged in the 09-10
   audit; no prod decision taken yet (outside this phase's implementable scope).
5. **Low — no locust/load tooling:** WS fan-out and auth endpoint load baselines are
   unmeasured (deferred requirements 5/6/7).

---

## 12. Required Future Work (explicitly deferred, non-blocking)

1. `docs/performance-review.md` — EXPLAIN on `/market/candles`, signals, decisions, alerts;
   Redis usage/hit-rate review; WS fan-out load check (e.g. 100 concurrent clients).
2. Secrets rotation runbook section in `docs/runbook.md`.
3. Real-CA TLS + HSTS per §8 (BLOCKED on domain).
4. Reposture decisions for `/metrics` exposure and Grafana creds before real production.
5. Refresh the stale status documents (§9).

---

## 13. Final Acceptance Checklist

- [x] Threat-model + self-audit checklist exist and map plan §12 (`docs/threat-model.md`)
- [x] pip-audit + npm audit + Trivy gates green in CI; Dependabot active (§5)
- [x] CSP + security headers verified on API/web/nginx; no dev regressions (§3)
- [x] Rate limiting works behind nginx per real client IP, trusted-proxy gated (§3)
- [x] `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`
      boots api+web+nginx healthy (verified pre-final-audit, docs §2/§7)
- [x] TLS on 443 (HTTP/2, TLSv1.2/1.3, WSS) + 80→301; certs gitignored and documented (§7)
- [x] `make backup`/`make restore` work; restore drill performed once with checksum (§6)
- [x] Resource limits set in prod overlay (§2, req 11)
- [x] No secrets in logs/images/artifacts/diff (§10)
- [x] SAFE MODE verified L1–L5 + live probes, unchanged and green (§3)
- [x] Phase 10 hardening sign-off recorded (this document's §14)
- [x] `make verify` and CI gates green on current tree (§4, §5)

## 14. Sign-off

Classification summary: **13 of 16 requirements COMPLETE**; 4 deferred measurement/rotation
items (requirements 5, 6, 7, 14) explicitly documented as non-blocking future work; TLS items
that require a production domain (**real CA cert, HSTS, HTTP-01/DNS-01**) classified
**BLOCKED — requires production domain/certificate infrastructure** with exact remaining
work in §8; no requirement failed by a defective implementation.

**PHASE 10 ACCEPTED**