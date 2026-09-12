# Phase 10 — Base Image Security

**Date:** 2026-09-11 · **Scope:** F-04 only (CVE-2026-14456)

## 1. Finding

| Attribute | Value |
|-----------|-------|
| Advisory | **CVE-2026-14456** — OpenSSL QUIC-server unbounded memory DoS |
| Audit ID | F-04 (HIGH) in `docs/dependency-security-audit.md` §4/§7/§8 |
| Affected components | `openssl`, `libcrypto3`, `libssl3` |
| Fix version (Alpine) | `3.5.8-r0` (verified present in the Alpine v3.24 `main` repo) |
| Fix version (Debian) | `3.5.7-1~deb13u2`+ (backend base pulled fresh) |

Source of truth for "which layer": Trivy (`aquasec/trivy:0.61.1`, the same version
and flags as the CI gate) reported the vulnerable **OS packages baked into the base
image layers** of the built project images — not any Python/Node application
dependency, not any project file.

## 2. Affected Images

The project builds exactly two image families (`frontend/Dockerfile`,
`backend/Dockerfile`; workers build from the backend Dockerfile context):

| Image family | Base (before) | Was the CVE present? |
|--------------|---------------|----------------------|
| `forex-ai-web` (prod target) | `node:24-alpine` → Alpine 3.24.1, baked `libcrypto3`/`libssl3` 3.5.7-r0 | **YES** (originally reported) |
| `forex-ai-api` (prod target) | `python:3.12-slim` → Debian 13.6, baked openssl 3.5.6-1~deb13u2 | YES at audit time (older cached base layer); already cleared by a fresh `python:3.12-slim` pull before this remediation |
| worker-* (ingest/agents/content/orchestrator/alerts) | same backend Dockerfile (`python:3.12-slim`) | Same as `forex-ai-api` (no separate Dockerfile; verified share the base) |

The external `nginx:1.27-alpine` image (Alpine 3.21.3, pulled from Docker Hub) also
carries `libcrypto3`/`libssl3`, but it is **not** a project-built image and is **not**
part of the CI scan policy (docker-build.yml scans only the two project images). It
is recorded here as an informational observation, not remediated in this phase
(out of F-04 scope; tracked in §10).

## 3. Root Cause

- The `node:24-alpine` tag floats, but Docker Hub's manifest **bakes the OS packages
  at image build time** (it runs the equivalent of `apk add` inside its build). The
  manifest in use shipped `openssl 3.5.7-r0`, whereas the Alpine v3.24 `main`
  repository had already published the patched `3.5.8-r0`.
- Verification (not guessed): the freshly pulled `node:24-alpine` was inspected
  (`/etc/alpine-release` → `3.24.1`, `apk info` → `libcrypto3-3.5.7-r0`), and the
  Alpine v3.24 `main` APKINDEX was queried (`curl` + `APKINDEX` parse) →
  `libcrypto3-3.5.8-r0`, `libssl3-3.5.8-r0`, `openssl-3.5.8-r0`.
- A plain rebuild with the same floating tag therefore would **not** have fixed it;
  the fix had to be applied inside our image build.

## 4. Remediation

Single, targeted change in `frontend/Dockerfile`, **prod stage only**:

```dockerfile
RUN apk add --no-cache --upgrade openssl libcrypto3 libssl3
```

- Upgrades exactly the three vulnerable packages from the pin-set defined by the
  base image's `/etc/apk/repositories` (`v3.24/main` + `community`). Dry-run showed
  only those three packages change (`3.5.7-r0 → 3.5.8-r0`); **no blanket `apk
  upgrade`**, no node/npm/app dependency change, no distro switch.
- Applied only to the `prod` stage (the stage that ships). The `deps`/`builder`
  intermediate stages and the `dev` stage are not deployed and are outside the CI
  scan policy; they were deliberately left untouched to keep the diff minimal.
- Nothing else changed: no nginx/TLS/HSTS configuration, no compose files, no
  Python/Node versions, no app/trading logic, no health checks, no resource limits,
  no non-root user (still `USER node`).

The backend `forex-ai-api` image needed **no Dockerfile change**: the previously
cached vulnerable base layer is gone once the image is rebuilt, because a fresh
`python:3.12-slim` pull now resolves to a patched Debian build (prod rescan = 0 OS
findings).

## 5. Base Image Versions

| Aspect | Before | After |
|--------|--------|-------|
| Frontend base | `node:24-alpine` (Alpine 3.24.1) | **unchanged** `node:24-alpine` (Alpine 3.24.1) + in-build apk upgrade |
| Frontend runtime tag | `forex-ai-web` prod target | rebuilt, same tag |
| Backend base | `python:3.12-slim` (Debian 13.6, cached layer with old openssl) | `python:3.12-slim` (Debian 13.6, fresh layer, patched openssl) |
| Node version | 24 | **unchanged** |
| Python version | 3.12 | **unchanged** |
| Application deps | unchanged | **unchanged** |
| Distribution | Alpine (frontend) / Debian (backend) | **unchanged** (no OS switch) |

## 6. OpenSSL Versions

| Image | Before | After |
|-------|--------|-------|
| `forex-ai-web` prod | `libcrypto3-3.5.7-r0`, `libssl3-3.5.7-r0` | **`libcrypto3-3.5.8-r0`, `libssl3-3.5.8-r0`**, `openssl-3.5.8-r0` |
| `forex-ai-api` prod | 3.5.6-1~deb13u2 (old cached layer) | patched (fresh `python:3.12-slim`); rescan = 0 OS findings |

Verified live inside the running prod container: `docker exec forex-ai-web-1 apk
info -a libcrypto3` → `libcrypto3-3.5.8-r0`.

## 7. Trivy Before/After

Policy = project CI policy: `trivy image --exit-code 1 --severity HIGH,CRITICAL
--ignore-unfixed` (0.61.1, pinned by digest).

| Image | Before | After | Raw scan (no ignorefile) after |
|-------|--------|-------|-------------------------------|
| `forex-ai-web` | 9 HIGH / 0 CRITICAL (incl. `CVE-2026-14456`) | **8 HIGH / 0 CRITICAL** (F-02/F-03/F-05 only), exit 0 | **CVE-2026-14456 absent**; same 8 node-pkg HIGH remain |
| `forex-ai-api` | 0 (fresh build; audit-era cached layer had 3 HIGH) | 0 HIGH / 0 CRITICAL, exit 0 | 0 |

`CVE-2026-14456` was **removed from `.trivyignore`** (an exception is no longer
needed). The remaining 8 entries (postcss×2, sharp×2, brace-expansion×2,
ip-address, tar) are the pre-existing documented F-02/F-03/F-05 exceptions — none
were removed or weakened, and no new HIGH/CRITICAL appeared.

## 8. Application Verification

| Check | Command | Result |
|-------|---------|--------|
| Backend unit (+ coverage gate) | `pytest tests/unit --cov=app --cov-report=term-missing` | **463 passed**, gate exit 0 |
| Backend integration | `pytest tests/integration` (real PostgreSQL + Redis) | **102 passed** |
| Frontend Vitest | `npx vitest run` | **66 passed** |
| ESLint | `npm run lint` | exit 0 |
| TypeScript | `npx tsc --noEmit` | exit 0 |
| Next.js build | `npm run build` | exit 0 (6 routes) |
| Prod image build | `docker build --target prod...` + `make prod-up` | exit 0; overlay healthy |

No application, test, or CI workflow file was changed by this remediation — the
Dockerfile RUN line affects only the image OS layer.

## 9. SAFE MODE Verification

Prod overlay (`make prod-up` — `docker compose -f docker-compose.yml -f
docker-compose.prod.yml up -d --build`), all 12 services healthy:

- `GET /health/ready` (via HTTPS edge) → `{"status":"ok","mode":"safe"}`
- `GET /system/status` → `app_env: prod`, **`trading_mode: safe`**, **`safe_mode: true`**,
  database/redis/migrations all `ok`
- SAFE MODE is enforced by the backend application (unchanged code); the Dockerfile
  change cannot alter it. Re-verified live.

## 10. Remaining Vulnerabilities

- **0 CRITICAL.**
- **8 HIGH, all Node `node-pkg` tooling, `--ignore-unfixed`**, each justified in
  `.trivyignore` (`docs/phase10-ci-security.md` §8):
  - `postcss@8.4.31` — F-02 (build-time CSS pipeline of Next)
  - `sharp@0.34.5` — F-03 (no `next/image` usage)
  - `brace-expansion@5.0.7` (×2), `ip-address@10.2.0`, `tar@7.5.19` — F-05
    (dev/build tooling; stale layer)
- Informational, out of CI scan scope: the external `nginx:1.27-alpine` image
  (Alpine 3.21.3) carries `libcrypto3`/`libssl3` and would need its own scan+bump
  via a future Dependabot `docker` entry / separate gate if the operator wants it
  gated; it is not introduced or modified by this change. (It is also not covered
  by the project CI and not part of the audited F-04 finding.)

## 11. Rollback

- Revert the single change: remove `RUN apk add --no-cache --upgrade openssl
  libcrypto3 libssl3` from `frontend/Dockerfile` (prod stage).
- The image then returns to the floating `node:24-alpine` base behaviour; if the
  base manifest is still on a pre-3.5.8 build, CVE-2026-14456 would return — in that
  case re-add its `.trivyignore` entry **only** with the original justification and
  update `docs/dependency-security-audit.md`/`docs/phase10-ci-security.md` to match.
- No database, volume, or state migration is involved; no application data is at
  risk. Rebuild and rescan are the only steps.

## 12. Acceptance Checklist

| Requirement | Status |
|-------------|--------|
| F-04 root cause identified exactly (base layer baked openssl, Alpine v3.24 repo has 3.5.8-r0) | COMPLETE |
| Minimal targeted base fix applied (`apk add -u` on openssl family, prod stage only) | COMPLETE |
| No broad dep upgrade, no OS/distro switch, no app logic, TLS, HSTS, CA, or trading changes | COMPLETE |
| Prod image built via project procedure | COMPLETE |
| Trivy rescan per CI policy (exit-code 1, HIGH/CRITICAL, ignore-unfixed) | COMPLETE |
| CVE-2026-14456 gone (raw scan without ignorefile) | COMPLETE |
| No new HIGH/CRITICAL introduced | COMPLETE |
| `.trivyignore` entry for CVE-2026-14456 removed; unrelated exceptions preserved | COMPLETE |
| Backend unit + integration / Vitest / ESLint / tsc / Next build all green | COMPLETE |
| Prod overlay started; all services healthy | COMPLETE |
| `/health/ready` and `/system/status` verified | COMPLETE |
| `trading_mode=safe`, `safe_mode=true` | COMPLETE |
| No unintended host ports (only nginx 80/443) | COMPLETE |
| Staging TLS still works (HTTP 301 → HTTPS, HTTP/2, self-signed CN=localhost) | COMPLETE |
| Backup/restore tooling unaffected (`make backup` OK; gzip/sha256/18 tables verified) | COMPLETE |
| Git diff limited to intended files | COMPLETE — see report |

Note: `nginx:1.27-alpine` (external image) is documented as out-of-scope in §10; no
nginx/TLS config changed per constraints.