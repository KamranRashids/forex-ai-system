# Dependency Security Audit

> Phase 10 — ITEM 2. Audit of the dependency and supply-chain configuration for the
> Multi-Agent Forex AI System. Primary focus is audit and documentation; no dependency
> changes are applied in this step. Follows `PHASE_10_AUDIT.md` item 2 and the
> `docs/threat-model.md` Dependency / Supply-Chain section (``## 22``).

## 1. Scope

This audit covers:

- Python dependency definitions and resolved versions (backend)
- Node/frontend dependencies and the lockfile
- Docker base images and multi-stage build inputs
- GitHub Actions / CI supply-chain configuration
- Dependabot configuration
- Known vulnerable / outdated dependency findings, per-finding severity,
  exploitability in this project, and whether remediation is required for Phase 10

Reviewed configuration files:

- `backend/pyproject.toml`
- `frontend/package.json`
- `frontend/package-lock.json` (present — the only dependency lockfile in the repo)
- `backend/Dockerfile`, `frontend/Dockerfile`
- `.github/workflows/backend-ci.yml`, `.github/workflows/frontend-ci.yml`,
  `.github/workflows/docker-build.yml`
- `.github/dependabot.yml`
- `docker-compose.yml`, `docker-compose.prod.yml` (base-image tags)

Classification tokens used: COMPLETE, PASS, LOW, MEDIUM, HIGH, CRITICAL,
NEEDS VERIFICATION, TOOL NOT AVAILABLE.

## 2. Python Dependency Audit

- Python dependency model: `backend/pyproject.toml` with `[project] dependencies`
  and `[project.optional-dependencies] dev`. **No lockfile exists** (no
  `requirements.txt`, `poetry.lock`, `Pipfile.lock`, or `uv.lock`). Versions are
  constrained by range only, so `pip install` is not reproducible. **PASS** that
  ranges exist; **MEDIUM** that there is no committed lock/`--require-hashes`.
- Tool status: `pip-audit` IS NOT installed in the project venv (or system). It was
  executed from an isolated throwaway venv under `/tmp` (no project or global
  modification) against the 71 packages resolved in `backend/.venv`
  (`pip freeze`, editable project lines excluded).
- Audit tool: `pip-audit` (pip-audit), executed 2026-09-11.
- Exit code 1 = vulnerabilities present. Result: **71 packages scanned, 1 advisory,
  2 duplicate rows** (pytest listed twice — dev dependency, also under test extras).

| Package     | Version | Advisory          | Severity | Fix      | In-project exploitability |
|-------------|---------|-------------------|----------|----------|---------------------------|
| pytest      | 8.4.2   | PYSEC-2026-1845   | (informational severity field unset; classified MEDIUM by project) | 9.0.3 | Dev-only. Local `/tmp/pytest-of-{user}` directory-name pattern weakness on UNIX. Not present in runtime images. |

- All runtime production dependencies scanned clean (fastapi, starlette, uvicorn,
  pydantic, sqlalchemy, asyncpg, redis, cryptography, PyJWT, httpx, pandas, numpy,
  etc.): **PASS**.
- **Conclusion: Python runtime dependencies have no known vulnerabilities.**
  The single finding is dev-only (LOW/optional remediation).

## 3. Node Dependency Audit

- Dependency model: `frontend/package.json` (`^15.1.6` next "next", `^19.0.0`
  react/react-dom, `^5.7.3` typescript, `^22.10.5` @types/node, plus eslint/vitest
  dev tooling). **`frontend/package-lock.json` IS present** — deterministic for
  `npm ci` in CI and Docker builds. **PASS** for lockfile presence.
- Tool status: `npm audit` IS available (`npm`, `node` installed). Ran `npm audit
  --json` (all deps) and `npm audit --omit=dev --json` (production only).
- Exit code 1 = vulnerabilities present. Result: **461 total packages**, 4 findings
  (all deps) / 3 findings (production only).

### Production dependency findings

| Level    | Package  | Resolved | Advisory | Fix    | In-project exploitability |
|----------|----------|----------|----------|--------|---------------------------|
| CRITICAL | next     | 15.5.23  | GHSA-p293-qw3h-jr36 — Unauthenticated RCE on windows-hosted servers (CWE-22, CVSS 3.1 9.0); GHSA-2xp9-vwfh-vxw4 — Unauthenticated RCE in Image Optimization API when AVIF files are used | >=15.5.24 | **Relevant.** Unauthenticated attacker-triggerable RCE in the Next.js server the web container runs (dev `web` on :3000, and prod standalone server). Note the Windows-hosted vector matches this repo (hosted on Windows/WSL). |
| HIGH      | postcss  | 8.4.31 (transitive via next) | GHSA-qx2v-qp2m-jg93 (XSS in CSS output); GHSA-6g55-p6wh-862q / GHSA-fxqj-rqcc-2cmp / GHSA-r28c-9q8g-f849 (sourceMappingURL path traversal / .map disclosure) | >=8.5.12 (fixed transitively by next >=15.5.24) | Low direct reachability (styles are statically compiled at build). Resolved by the Next.js bump. |
| HIGH      | sharp    | 0.34.5 (transitive via next) | GHSA-f88m-g3jw-g9cj (libvips CVEs), GHSA-rgj7-g3m4-5g8c (libheif) | 0.35.4 (fixed direct dep, in Next's declared range) | Only used by Next image optimization. **RESOLVED 2026-09-11** — upgraded to `^0.35.4` as a direct dependency; see `docs/phase10-sharp-remediation.md`. |

### Development dependency finding

| Level | Package | Resolved | Advisory | Fix | In-project exploitability |
|-------|---------|----------|----------|-----|---------------------------|
| HIGH  | js-yaml | 4.3.1 (transitive via `@eslint/eslintrc`, eslint toolchain) | js-yaml DoS in YAML parsing (maxTotalMergeKeys / empty merge sources) | 4.3.2 / 3.15.2 | Dev/build-time only. Not bundled into runtime output. Not exploitable at runtime in this project. |

## 4. Container Image Audit

- Tool status: `trivy` IS NOT installed on the host. Executed as a one-shot,
  throwaway container (`aquasec/trivy:0.61.1`) over the Docker socket — no install
  performed, no project change. Ran `image --ignore-unfixed --severity
  HIGH,CRITICAL` against the two built artifacts actually run by this stack:
  `forex-ai-api:latest` and `forex-ai-web:latest`.
- Base images: `backend/Dockerfile` → `python:3.12-slim` (resolved to Debian 13.6);
  `frontend/Dockerfile` → `node:24-alpine` (resolved to Alpine 3.24.1), multi-stage
  `npm ci`. Compose runtime tags: `postgres:16-alpine`, `redis:7-alpine`,
  `prom/prometheus:v2.53.0`, `grafana/grafana:11.1.1`, `nginx:1.27-alpine` (prod).

| Image   | OS/class | Findings |
|---------|----------|----------|
| `forex-ai-api:latest` (Debian 13.6) | OS pkgs | 0 HIGH — patched base (`python:3.12-slim` now resolves to openssl 3.5.7+); **F-04 resolved** (fresh base pull) |
| `forex-ai-web:latest` (Alpine 3.24.1) | OS pkgs | **F-04 resolved** — `libcrypto3`/`libssl3` upgraded to 3.5.8-r0 via targeted `apk add` in Dockerfile prod stage |
| `forex-ai-web:latest` | Node.js (image-bundled deps) | 2 CRITICAL (next 15.5.23 — the same two Next RCE advisories as `npm audit`); 9 HIGH — brace-expansion 5.0.7 (CVE-2026-14257, CVE-2026-69152, DoS), ip-address 10.2.0 (CVE-2026-69192, SSRF-adjacent parse inconsistency), js-yaml 4.3.1, postcss 8.4.31, sharp 0.34.5, tar 7.5.19 (CVE-2026-73566, DoS) |

> **Note (2026-09-11):** the `js-yaml` finding in this historical row was from the
> original scan (likely a dev-target or pre-`.dockerignore` image). On the current
> `--target prod` image, `js-yaml` does **not** appear (Trivy on `forex-ai-web:verify`
> = 0 HIGH for js-yaml). It remains a dev-only concern in `npm ls` / `npm audit`
> (`@eslint/eslintrc` transitive); not flagged by Trivy on any prod image.

- `ip-address@10.2.0` was flagged by the original trivy run against the image then in
  use; it is **not present in the current `npm ls` tree and not in `package-lock.json`**.
  **Verified (2026-09-11):** on a freshly built prod image (`forex-ai-web:verify`,
  project `docker build --target prod`), `ip-address`, `brace-expansion`, and `tar`
  exist only inside `/usr/local/lib/node_modules/npm/` — the **npm CLI bundled into the
  `node:24-alpine` base image** (npm v11.19.0), not the application layer (`/app` has
  none). No runtime code path executes the npm CLI. They are now correctly documented as
  a **base-image/tooling exception (F-05)**, not an application dependency or stale
  lockfile artifact. See `docs/phase10-remaining-dependency-findings.md` §1.
- ~~OpenSSL CVE-2026-14456 is a QUIC-server DoS. This application never accepts TLS~~ **RESOLVED** (2026-09-11):
  frontend prod stage patched to `openssl 3.5.8-r0` via targeted `apk add`; backend base
  already patched by fresh `python:3.12-slim` pull. Both images rescan clean. See
  `docs/phase10-base-image-security.md`.

## 5. GitHub Actions / CI Supply Chain

Workflows reviewed: `backend-ci.yml`, `frontend-ci.yml`, `docker-build.yml`.

| Item | Status | Detail |
|------|--------|--------|
| Official-actions usage | PASS | Only GitHub-owned actions: `actions/checkout@v4`, `actions/setup-python@v5`, `actions/setup-node@v4`. No third-party marketplace actions. |
| Actions pinned by major tag only | MEDIUM | Tags are mutable; no SHA pinning (`checkout@v4` not `@<sha>`). Standard hardening is to pin to a full commit SHA with a comment noting the tag. |
| `permissions:` declarations | MEDIUM | No workflow sets `permissions:`. `docker-build.yml` builds but never pushes/logs into a registry, and CI uses no real production secrets, so practical impact is currently low — should still be pinned to `contents: read` to be least-privilege. |
| Registry/index pinning | PASS | Builds use upstream PyPI/default npm registry and `npm ci` (exact from lockfile). Python side has no lockfile (see section 2). |
| Secrets handling | PASS | Only CI-scoped `TEST_DATABASE_URL` (throwaway Postgres credential) is used. No production secrets in workflows. |
| Hash-pinning of pip | MEDIUM | Without a lockfile there is no `--require-hashes`; recommend a committed lock or hashed requirements before production builds. |
| Freshness | PASS | `composite` actions are all recent major versions; Dependabot covers `github-actions` monthly. |

## 6. Dependabot

- Status: **COMPLETE**.
- `.github/dependabot.yml` enables: `pip` (backend) weekly, `npm` (frontend) weekly,
  `docker` (backend, frontend Dockerfiles) monthly, `github-actions` monthly.
- Coverage notes:
  - The npm entry keeps `package-lock.json` in sync (deterministic). **PASS**.
  - The pip entry edits `pyproject.toml` ranges only (no lockfile to update); update
    PRs are not byte-reproducible without a lock. **MEDIUM** (ties to section 2).
  - Runtime service images consumed by compose (`postgres:16-alpine`,
    `redis:7-alpine`, `prom/prometheus`, `grafana/grafana`, `nginx:1.27-alpine`)
    are NOT tracked by Dependabot — they are first-party images only tracked for the
    two app Dockerfiles. Tag bumps for those images rely on manual review.
    **MEDIUM** (documenting a coverage gap, not a vulnerability).

## 7. Findings

| ID   | Severity | Source        | Finding                                      | Package / artifact      | Phase 10 impact |
|------|----------|---------------|----------------------------------------------|-------------------------|-----------------|
| F-01 | CRITICAL | npm audit, trivy | Unauthenticated RCE — Next.js server (2 advisories: Windows-hosted; Image-Optimization AVIF) | next 15.5.23             | REQUIRED         |
| F-02 | HIGH     | npm audit, trivy | Path traversal / .map disclosure + XSS in CSS output | postcss 8.4.31 (via next) | Fix via F-01    |
| F-03 | HIGH     | npm audit, trivy | libvips / libheif CVEs in sharp               | sharp 0.34.5 (via next)   | Fix via F-01 → **RESOLVED 2026-09-11** (sharp ^0.35.4 direct dep) |
| F-04 | HIGH     | trivy           | OpenSSL QUIC-server unbounded memory DoS     | libssl/libcrypto/openssl in both images | Required on image rebuild |
| F-05 | HIGH     | trivy           | DoS in brace-expansion / ip-address / tar — **all three vendored inside the npm CLI shipped in the `node:24-alpine` base image** (originally misattributed as dev tooling / stale layer) | brace-expansion 5.0.7, ip-address 10.2.0, tar 7.5.19 | ACCEPTED EXCEPTION verified — not app deps, not runtime-reachable |
| F-06 | MEDIUM   | pip-audit       | `/tmp/pytest-of-{user}` symlink/perms weakness on UNIX | pytest 8.4.2 (dev-only)   | Optional (dev)   |
| F-07 | MEDIUM   | repository      | Untagged/PyPI-range-only Python deps, no lockfile / `--require-hashes` | backend pyproject.toml   | **RESOLVED 2026-09-12** — hashed pip-tools locks (`requirements.lock` + `requirements-dev.lock`) wired into Docker/Makefile/CI with `--require-hashes` |
| F-08 | MEDIUM   | repository      | CI actions pinned by mutable tag; no `permissions:` block | .github/workflows/*      | Recommended     |
| F-09 | MEDIUM   | repository      | Dependabot does not track runtime base images | postgres/redis/prom/grafana/nginx tags | Documented gap |

## 8. Severity Assessment

- **CRITICAL (F-01)** — Real and directly relevant: an unauthenticated attacker can
  trigger RCE in the Next.js server this system ships (dev `web` on :3000 and prod
  standalone). The “windows-hosted servers” advisory matches the repo's own hosting
  platform. Fix is a patch-level, in-range upgrade (15.5.23 → >=15.5.24, `^15.1.6`
  already admits it); no code redesign, no trading logic, no SAFE MODE change.
- **HIGH (F-02, F-03)** — Real advisories, but reachable only through behaviour the
  project does not rely on (style emission at build time; image optimization
  pipeline). F-02 eliminated by F-01's fix; **F-03 RESOLVED (2026-09-11)** by the
  in-range `sharp@^0.35.4` direct-dependency upgrade.
- **HIGH (F-04)** — Real OS-level advisory but with **LOW in-project**~~exploitability~~. **RESOLVED (2026-09-11)** — the app never serves TLS/QUIC directly and the vulnerable libs are bundled system libs; fixed by a targeted base-package upgrade (frontend) and a fresh base pull (backend). See `docs/phase10-base-image-security.md`.
- **HIGH (F-05)** — Verified **base-image tooling**, not application dependencies:
  the affected packages exist only inside `/usr/local/lib/node_modules/npm/` (npm CLI
  bundled in `node:24-alpine`). The runtime `node server.js` process never executes
  them; attacker-reachable paths on the deployed edge do not involve npm. No app-side
  fix exists (upstream `node:24-alpine` must refresh its bundled npm, or the container
  could drop npm entirely — documented in `docs/phase10-remaining-dependency-findings.md`).
- **MEDIUM (F-06)** — Dev-only, local-account precondition. Not exploitable in the
  deployed system.
- **MEDIUM (F-07, F-08, F-09)** — Process/hygiene gaps, not active vulnerabilities.
- `js-yaml@4.3.1` (GHSA-2883-xcg3-v3hh, via `@eslint/eslintrc`) is **dev-only**:
  present in the dev dependency tree, **absent** from the runtime image and from the
  `npm audit --omit=dev` closure.

## 9. Recommended Remediation

Required for Phase 10:

1. **F-01 (REQUIRED):** Upgrade Next.js within its existing range and rebuild the
   web image. Exact command (single change, no code/design change):
   `cd frontend && npm install next@^15.5.24` (resolves to 15.5.25; also updates
   the committed `package-lock.json`), then `npm run build` + `npm run lint` +
   `vitest run` to verify, then rebuild `forex-ai-web`.
2. **F-04 (REQUIRED with image rebuild) — DONE (2026-09-11):** Frontend prod stage
   upgraded to `openssl/libcrypto3/libssl3 3.5.8-r0` via targeted `apk add`, backend
   base re-pulled (`python:3.12-slim` now patched), trivy rescan clean;**
   `.trivyignore` entry removed.

> Correction applied during remediation (evidence below): F-02/F-03 were expected to
> be fixed transitively by `next >=15.5.24`. In practice Next.js 15.5.25 still pins
> `postcss@8.4.31` (and had limited `sharp@0.34.5` to its optional range), so the
> advisories remained reported by audit tools. They are **build-time (postcss)** and
> **unreachable (sharp — this app does not use `next/image`)**, so the CRITICAL RCE
> (F-01) was the runtime-relevant risk. **F-03 was subsequently closed (2026-09-11)**
> by upgrading sharp to `^0.35.4` — a fix `next@15.5.25` already declares compatible.

Recommended (not blocking):

3. **F-06 (optional):** `pip install -U pytest` in the dev venv (→ 9.0.3) when
   convenient; dev-only.
4. **F-07:** **DONE (2026-09-12)** — hashed pip-tools locks committed
   (`backend/requirements.lock` runtime, `backend/requirements-dev.lock` dev)
   and wired into the Dockerfile, Makefile, and CI with `pip --require-hashes`;
   verified clean-installable, pip-audit-clean, and gate-passing. Full record:
   `docs/phase10-python-lockfile.md`.
5. **F-08:** Pin GitHub Actions to full commit SHAs (with tag comment) and add
   `permissions: contents: read` to each workflow.
6. **F-09:** Track runtime image tags in Dependabot (add `docker` ecosystems for the
   compose `infra` dirs) or adopt scheduled digest pinning.

## 10. Phase 10 Required Actions

- Apply F-01 (Next.js in-range bump) — **required**, single in-range patch, no
  behavioural change.
- ~~Rebuild both images against fixed base layers and re-scan with Trivy — **required**~~ **DONE** (2026-09-11): frontend
  prod stage patched (`apk add --no-cache --upgrade openssl libcrypto3 libssl3` → 3.5.8-r0);
  backend base `python:3.12-slim` already resolved to a patched build. Full Trivy rescan
  confirms CVE-2026-14456 gone from both prod images. See `docs/phase10-base-image-security.md`.
- ~~Add automated gates (pip-audit + npm audit + Trivy) to CI — already listed in~~ **DONE** (`PHASE_10_AUDIT.md` §2
  follow-ups, F-07/F-08 recommendations folded in). See `docs/phase10-ci-security.md`.
- No other dependency change is required. No package was upgraded, downgraded,
  added, or removed during this audit step (F-04 remediation was a targeted
  base-package fix, not a Node/Python/app dependency change).

## 11. Verification Evidence

- `pip freeze` of `backend/.venv` — 71 packages audited (editable project entry
  excluded from scanning input).
- `pip-audit -r <pip freeze output> --format json` — exit 1; only PYSEC-2026-1845
  (pytest, dev-only). Full JSON: `/tmp/pip_audit.json` (throwaway).
- `npm audit --json` (all) — exit 1; 4 findings (js-yaml dev, postcss, sharp, next);
  `npm audit --omit=dev --json` — exit 1; 3 findings (postcss, sharp, next) before the
  F-03 fix; **after `sharp@^0.35.4`: 2 findings (postcss, next), exit 0 with the
  postcss-only allowlist** — sharp gone.
  Full JSON: `/tmp/npm_audit.json`, `/tmp/npm_audit_prod.json` (throwaway).
- `aquasec/trivy:0.61.1 image --ignore-unfixed --severity HIGH,CRITICAL` on
  `forex-ai-api:latest` (3 HIGH) and `forex-ai-web:latest` (2 HIGH OS + 2 CRITICAL +
  9 HIGH Node). Full JSON: `/tmp/trivy_api.json`, `/tmp/trivy_web.json` (throwaway).
- Repository evidence: `frontend/package-lock.json` present; `npm ls js-yaml`
  → `@eslint/eslintrc`; `npm ls ip-address` → not in current tree;
  `npm ls sharp` → next → sharp@0.34.5; Dockerfiles/compose/Dependabot read in full.
- No project files were modified by this audit. Audit artifacts live only in `/tmp`.

## 12. Acceptance Checklist

| Requirement | Status |
|-------------|--------|
| Python deps audited with pip-audit (throwaway venv, not global, no project change) | COMPLETE |
| Node deps audited with npm audit (production and full) | COMPLETE |
| Docker images scanned with Trivy (one-shot container, no host install) | COMPLETE |
| GitHub Actions / CI supply chain reviewed | COMPLETE |
| Dependabot configuration reviewed | COMPLETE |
| Known vulnerable/outdated deps enumerated with severity + exploitability + Phase 10 need | COMPLETE |
| No silent installs into the project or machine | COMPLETE |
| No dependency upgraded/downgraded/added/removed during the audit | COMPLETE |
| Required change identified and justified (F-01 inbound-range Next.js bump) | COMPLETE |
| Runtime Python dependencies have no known vulnerabilities | PASS |
| F-01 (Next.js CRITICAL) fixed by a single in-range bump | COMPLETE — see remediation section 13 |
| F-04 cleared by image rebuild + trivy rescan | COMPLETE — both prod images patched and rescanned clean |

## 13. Remediation Applied — F-01 (Next.js)

Applied 2026-09-11 as the minimum required change; nothing else was modified.

### Original vulnerable version
- `next@15.5.23` (resolved from declared range `^15.1.6`)
- Advisories: GHSA-p293-qw3h-jr36 (unauthenticated RCE, Windows-hosted, CVSS 9.0),
  GHSA-2xp9-vwfh-vxw4 (unauthenticated RCE via Image Optimization API, AVIF)

### Patched version
- `next@15.5.25`, range updated to `^15.5.25` in `frontend/package.json`
- `frontend/package-lock.json` updated (single package bump by `npm`, in-range)

### Commands used
- `npm install next@^15.5.24 --no-audit --no-fund` → "changed 3 packages"
  (package.json range normalized to `^15.5.25` = latest patch admitted by the range)
- Verification: `node -p "require('./node_modules/next/package.json').version"` → `15.5.25`
- No overrides, no force flags, no other packages touched.
  `npm ls` reports a clean tree (no invalid/extraneous/UNMET entries).

### Test / build results (all on Next 15.5.25)
| Check | Command | Result |
|-------|---------|--------|
| Unit tests | `npm test` (vitest run) | 66/66 passed (4 files), exit 0 |
| Lint | `npm run lint` (eslint .) | exit 0, clean |
| Type check | `npx tsc --noEmit` | exit 0, clean |
| Production build | `npm run build` (next build) | Success, 6 routes, exit 0 |

### Docker image rebuild
- Build: `docker compose build web` (existing compose setup, `target: dev`, image
  `forex-ai-web:latest`), exit 0. `.next` output included the rebuilt 15.5.25 bundle.

### Trivy result (rebuilt `forex-ai-web:latest`, HIGH/CRITICAL, `--ignore-unfixed`)
Before: **13** findings — 2 CRITICAL (next RCEs) + 9 HIGH (Node) + 2 HIGH (OS openssl).
After: **11** findings — **0 CRITICAL** + 9 HIGH (Node) + 2 HIGH (OS openssl).

### F-01 / F-02 / F-03 / F-04 / F-05 status
| ID | Status | Evidence |
|----|--------|----------|
| F-01 (next CRITICAL RCE) | **RESOLVED** | next 15.5.25 no longer flagged by `npm audit` or Trivy; both CRITICAL advisories gone from the rebuilt image |
| F-02 (postcss HIGH) | **ACCEPTED EXCEPTION** — verified build-time only | `postcss@8.4.31` is hard-pinned by `next@15.5.25` and ships at `/app/node_modules/next/node_modules/postcss` in the final image, but **no module under `/app/.next/server` references postcss** (CSS is compiled at build; see `docs/phase10-remaining-dependency-findings.md` §2). No attacker-controlled CSS is ever parsed. Next 15 pins `"postcss": "8.4.31"` exactly; the only upstream fix is a Next major (16.x), which is not a minimal change. |
| F-03 (sharp HIGH) | **RESOLVED (2026-09-11)** | `sharp` upgraded from `0.34.5` to **`^0.35.4`** (direct dependency — `frontend/package.json`), inside `next@15.5.25`'s declared `"^0.34.3 \|\| ^0.35.4"` range; deduped via npm (`next → sharp@0.35.4`). GHSA-f88m-g3jw-g9cj + GHSA-rgj7-g3m4-5g8c gone from raw Trivy, npm IDs 1124066/1193725 gone from `npm audit --omit=dev`. `.trivyignore` + npm-audit allowlist entries removed; both CI gates pass. See `docs/phase10-sharp-remediation.md` |
| F-04 (OpenSSL QUIC DoS) | **RESOLVED (2026-09-11)** | frontend prod stage upgrades `openssl`/`libcrypto3`/`libssl3` → 3.5.8-r0; raw rescan (no ignorefile) shows CVE-2026-14456 gone; `.trivyignore` entry removed; backend base `python:3.12-slim` already patched. See `docs/phase10-base-image-security.md` |
| F-05 (bc/i/tar Node-tooling HIGH) | **ACCEPTED EXCEPTION** — misattributed, now correctly scoped | `brace-expansion@5.0.7`, `ip-address@10.2.0`, `tar@7.5.19` are **NOT application dependencies** (absent from `npm ls`, absent from `package-lock.json`, absent from `/app`). They live in `/usr/local/lib/node_modules/npm/node_modules/` — the npm CLI bundled into the `node:24-alpine` base image (npm v11.19.0). No runtime code path executes the npm CLI (`CMD ["node","server.js"]`, `USER node`). No app-level fix exists; only an upstream `node:24-alpine` npm refresh removes them (or deleting `/usr/local/lib/node_modules/npm` from the image, documented in `docs/phase10-remaining-dependency-findings.md` §1) |

### Remaining HIGH/CRITICAL findings after remediation (Trivy raw scan, `forex-ai-web:sharp`)
- **0 HIGH (OS)** — CVE-2026-14456 resolved on both images.
- **6 HIGH / 0 CRITICAL (Node)** — all covered by `.trivyignore`, each verified against
  the freshly built prod image (`forex-ai-web:sharp`, project `docker build --target prod`):
  - `postcss@8.4.31` — 2 Trivy CVEs (CVE-2026-45623, CVE-2026-73646) / 4 GHSA via npm
    audit (GHSA-6g55-p6wh-862q, GHSA-r28c-9q8g-f849 high; GHSA-qx2v-qp2m-jg93,
    GHSA-fxqj-rqcc-2cmp moderate) — Next-pinned, build-time only → **F-02**
  - `brace-expansion@5.0.7` (CVE-2026-14257, CVE-2026-69152) — **npm CLI vendored** in
    base image, not an app dependency → **F-05**
  - `tar@7.5.19` (CVE-2026-73566) — **npm CLI vendored** in base image → **F-05**
  - `ip-address@10.2.0` (CVE-2026-69192) — **npm CLI vendored** in base image → **F-05**
- `sharp` no longer appears on the raw scan: GHSA-f88m-g3jw-g9cj + GHSA-rgj7-g3m4-5g8c
  cleared by the 0.35.4 upgrade (**F-03 RESOLVED**).
- npm audit (`--omit=dev`) additionally flags `next` **moderate** via postcss — a
  propagation of the F-02 exception, no separate advisory.
- `js-yaml@4.3.1` (GHSA-2883-xcg3-v3hh, dev-only, via `@eslint/eslintrc`) is **not**
  in the runtime image and not in the `--omit=dev` closure; dev-tree only.
- Remaining **0 CRITICAL**.

---

## 14. Security-hardening status (2026-09-11)

Complementary control implemented separately from the dependency audit — see
`docs/phase10-security-hardening.md`:

- **Nginx edge CSP + security headers** — DONE and verified (`server_tokens off`,
  `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`,
  `Permissions-Policy`, CSP with `default-src 'self'`, no `'unsafe-eval'`).
- **Per-client-IP rate limiting behind Nginx** — DONE and verified: `TRUSTED_PROXIES`
  gated `client_ip` recovery (`app/core/config.py`, `app/api/deps.py`), Nginx sets
  forwarded headers and strips inbound ones, prod subnet pinned to `172.28.0.0/16`;
  spoofed-header attempts through Nginx all hit the rate limit (no bypass), audit log
  records the real forwarded client IP (`172.28.0.1`).

These are process-level security controls (not audit findings F-01…F-09, which remain
as scored above); they do not change any dependency verdicts.

---

## 15. CI enforcement status (2026-09-11)

The automated gates from finding F-08 and the "Phase 10 Required Actions" are now
implemented — see `docs/phase10-ci-security.md` for the detailed record.

| Gate | CI location | Policy | Status |
|------|-------------|--------|--------|
| Python dependency audit | `backend-ci.yml` → `quality` → "Python dependency audit (pip-audit, runtime closure)" | **STRICT** over the production runtime closure (fresh venv, no `[dev]` extras, `--skip-editable`); any known vulnerability in a shipped package fails the build | **ENFORCED** — currently 0 findings (exit 0) |
| Node dependency audit | `frontend-ci.yml` → `quality` → "Node dependency audit" + "Enforce npm audit policy" | `npm audit --omit=dev`; allow-listed advisory IDs (F-02 postcss — build-time/unreachable) kept by `.github/scripts/check-npm-audit.cjs`; any NEW advisory (package or ID) fails the build | **ENFORCED** — currently only the 4 documented postcss exceptions (sharp IDs 1124066/1193725 removed 2026-09-11 with the fix), gate passes |
| Container image audit | `docker-build.yml` → scan steps | Trivy (pinned by digest to `0.61.1`) scans the exact locally-built prod images; `--exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed`; exceptions in `.trivyignore` (F-02/F-05, justified; F-04 and F-03 resolved and removed 2026-09-11) | **ENFORCED** — backend image 0 findings; frontend image 6 findings all ignored with justification, exit 0 |
| Third-party Actions pinned | all three workflows | immutable commit SHAs + human-readable version comments | **DONE** |
| Least-privilege permissions | all three workflows | `permissions: contents: read` at workflow top; no write scope granted | **DONE** |
| Frontend unit tests in CI | `frontend-ci.yml` → "Unit tests (Vitest)" | `npm run test` (`vitest run`) | **DONE** |

F-08 ("CI actions pinned to mutable tags, no permissions, no dependency gates")
is now closed. **F-07 is RESOLVED (2026-09-12)**: the Python closure is now fully
reproducible from the hashed pip-tools locks `backend/requirements.lock` (runtime,
56 pins) and `backend/requirements-dev.lock` (dev, 72 pins), installed everywhere
with `pip --require-hashes` (Dockerfile builder/dev stages, Makefile
`backend-venv`, CI `quality`/`integration`, and the pip-audit runtime-closure
venv). Clean-env install reproduces the validated venv exactly (0 version
changes), pip-audit finds no known vulnerabilities, and all backend quality gates
pass under the locked environment. See `docs/phase10-python-lockfile.md`.