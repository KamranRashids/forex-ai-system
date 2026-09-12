# Phase 10 — CI / Security Audit Integration

**Date:** 2026-09-11

## 1. Overview and status

Phase 10 follow-up that closes the CI gaps identified by the dependency and
supply-chain audit (`docs/dependency-security-audit.md`, §7 findings F-07/F-08 and
the "Phase 10 Required Actions"). It implements **minimum secure CI changes only**:
no CI redesign, no application or trading-logic changes, no Docker-architecture
changes, no broad dependency upgrades, and no new third-party Actions.

| Gap (audit) | Resolution | Status |
|-------------|-----------|--------|
| pip-audit not enforced in CI | runtime-closure gate in `backend-ci.yml` | ENFORCED |
| npm audit not enforced in CI | production-closure gate + allowlist policy in `frontend-ci.yml` | ENFORCED |
| Trivy image scanning not enforced in CI | scans exact built prod images in `docker-build.yml` | ENFORCED |
| GitHub Actions not SHA-pinned | all `uses:` refs pinned to immutable commit SHAs | DONE |
| Workflows lack restrictive permissions | `permissions: contents: read` on all workflows | DONE |
| `frontend-ci.yml` does not run Vitest | `npm run test` step added | DONE |

Scope constraints honored:

- No production domain → **no** real CA certificate, HSTS, Let's Encrypt, DNS-01 or
  HTTP-01 work in this phase.
- No application code, tests, or Dockerfiles were modified; **SAFE MODE is unchanged**
  and verified (see §9).
- No secrets or hard-coded credentials introduced; CI-only values that already
  existed (e.g. the integration job's `POSTGRES_PASSWORD: ci-password-only` test
  fixture) were left untouched.
- Nothing is committed, tagged, or released (this phase stops at the CI gate).

## 2. Python dependency audit — pip-audit

**Location:** `.github/workflows/backend-ci.yml`, job `quality`, step
"Python dependency audit (pip-audit, runtime closure)".

**Policy:** the gate is **strict** over the **production runtime closure** only:

```yaml
- name: Python dependency audit (pip-audit, runtime closure)
  run: |
    python -m venv /tmp/pip-audit-venv
    /tmp/pip-audit-venv/bin/pip install --quiet --upgrade pip
    /tmp/pip-audit-venv/bin/pip install --quiet -e .
    /tmp/pip-audit-venv/bin/pip install --quiet pip-audit
    /tmp/pip-audit-venv/bin/pip-audit --skip-editable
```

A fresh venv with **no `[dev]` extras** audits exactly what the deployed image
installs (same command the local audit used). A new known vulnerability in a shipped
package **fails the build**.

**Intentionally allowed exceptions:** dev-only findings such as
`PYSEC-2026-1845` (pytest, F-06) are excluded **by construction** — they are never
installed in the runtime venv the gate audits. This is the documented policy choice
instead of a suppression flag.

**Current state:** the runtime closure resolves to **0 known vulnerabilities**
(verified locally, §9). pip-audit itself is not pinned to a hash here; it is
installed latest into its own audit venv from the same PyPI index. The **audited
application closure is now the hashed, exact-pinned `requirements.lock`**
installed with `--require-hashes` (F-07 RESOLVED 2026-09-12;
`docs/phase10-python-lockfile.md`).

## 3. Node dependency audit — npm audit

**Location:** `.github/workflows/frontend-ci.yml`, job `quality`, steps
"Node dependency audit (npm audit, production deps)" and
"Enforce npm audit policy".

**Policy:** the gate reports `npm audit --omit=dev --json` (the production closure,
matching what `next build` bakes), then enforces an **allowlist policy** in
`.github/scripts/check-npm-audit.cjs`:

- Advisory IDs for **postcss** (`1117015`, `1124252`, `1130709`, `1139510`),
  transitive via `next`, are the **documented exceptions** (F-02 — build-time only).
- **Any other advisory (new package or new advisory ID) fails the build.**
- If `npm audit` output cannot be parsed (registry/network failure, malformed JSON)
  the gate **fails closed** so the audit stays reproducible and honest.
- If a future `next` bump clears postcss entirely, the gate **passes** with zero
  findings (no allowlist entries are forced).
- The **sharp** IDs (`1124066`, `1193725`) were in the allowlist while `sharp@0.34.5`
  shipped (F-03); they were **removed on 2026-09-11** when `sharp@^0.35.4` was applied
  as a direct dependency — a regressing sharp advisory now **fails** the gate. See
  `docs/phase10-sharp-remediation.md`.

The `continue-on-error: true` on the audit step is deliberate: npm's exit code flags
vulnerability presence, but the **policy decision belongs to the checked-in script**,
which is what CI actually gates on.

**Current state:** the production closure shows the 4 documented postcss exceptions and
nothing else; the enforce step passes (§9). The failure path was also verified: an
injected new advisory fails the gate with exit 1 (§9).

## 4. Container image audit — Trivy

**Location:** `.github/workflows/docker-build.yml`, steps "Scan backend image (Trivy)"
and "Scan frontend image (Trivy)".

**Policy:** each job builds the exact production image CI produces
(`docker build --target prod -t forex-ai-api:${{ github.sha }}` /
`-t forex-ai-web:${{ github.sha }}`) and scans **that image** (not a network ref,
not a generic `latest`):

```yaml
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.trivyignore:/.trivyignore" \
  aquasec/trivy@sha256:91bcccab7cb503127b0d792db7c652c4e556c9ff30eeb0908b6ae1980d7727ad image \
  --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed \
  --ignorefile /.trivyignore --quiet \
  <exact image tag>
```

- Trivy is pinned by **digest** (`sha256:91bc…77ad` = `0.61.1`, the version used by
  the original audit), not by mutable tag.
- `--exit-code 1` + `--severity HIGH,CRITICAL`: any new unignored HIGH/CRITICAL
  fails the build.
- `--ignore-unfixed`: a finding that has no available fix upstream (including base
  images that just have not been rebuilt) does not fail; exceptions for
  **fix-available** findings are listed explicitly in `.trivyignore` so they cannot
  silently drift.
- No secrets are uploaded and no artifact/attestation is published (least-privilege,
  §6). The scan runs one-shot in a disposable container — nothing installed on the
  runner.

**Current state (updated 2026-09-11):** backend image **0 findings**; frontend image
**8 HIGH / 0 CRITICAL**, all covered by `.trivyignore` entries with justifications
(§8, §9). **F-04 (CVE-2026-14456) was RESOLVED** via a targeted base-package upgrade
(`frontend/Dockerfile` prod stage, `openssl 3.5.8-r0`); its `.trivyignore` entry was
removed and the rescan is clean without any exception — see `docs/phase10-base-image-security.md`.

## 5. GitHub Actions SHA-pinning

All third-party Actions across the three workflows are pinned to immutable commit
SHAs with the human-readable tag as a comment:

| Action | Tag | Immutable SHA |
|--------|-----|---------------|
| `actions/checkout` | v4 | `11d5960a326750d5838078e36cf38b85af677262` |
| `actions/setup-python` | v5 | `a26af69be951a213d495a4c3e4e4022e16d87065` |
| `actions/setup-node` | v4 | `49933ea5288caeca8642d1e84afbd3f7d6820020` |

First-party shell commands and the pinned official images (`postgres:16-alpine`,
`redis:7-alpine`) are unchanged. Resolution of SHAs: GitHub `git refs/tags` API for
the exact tags in use at `2026-09-11`. Renovate/Dependabot (already configured for
`github-actions`) will open PRs to bump the tag and its SHA together.

## 6. Least-privilege workflow permissions

Each workflow declares the minimum at the top level:

```yaml
permissions:
  contents: read
```

- `backend-ci.yml`, `frontend-ci.yml`, `docker-build.yml` — read-only repository
  access. None of them push, tag, upload artifacts, open issues, or target a
  registry, so **no write scope is granted** (GitHub default
  `permissions: {}` for the token is overridden only with `contents: read` in the
  header so checkout and dependency-cache restoration work).

## 7. Frontend CI — Vitest

`frontend-ci.yml` previously ran lint/type-check/build but never executed the test
suite. Added, immediately after `npm ci`:

```yaml
- name: Unit tests (Vitest)
  run: npm run test   # vitest run
```

No duplicate expensive jobs were introduced — the Vitest run reuses the
already-installed `node_modules` in the same `quality` job.

## 8. Documented exceptions

Policy requires every suppression to carry a justification and a removal condition.

### npm audit allowlist (`.github/scripts/check-npm-audit.cjs`)

| Advisory ID | Package | Severity | Why allowed | Removal condition |
|-------------|---------|----------|-------------|-------------------|
| `1117015` | postcss (via next) | moderate | build-time CSS stringify; no runtime CSS input path | next/postcss upstream bump |
| `1124252` | postcss (via next) | high | source-map handling at build time only | next/postcss upstream bump |
| `1130709` | postcss (via next) | moderate | incomplete-fix follow-up, same build-time path | next/postcss upstream bump |
| `1139510` | postcss (via next) | high | source-map path traversal, build-time only | next/postcss upstream bump |

> **F-03 sharp entries REMOVED (2026-09-11):** `1124066` and `1193725` were retired
> from the allowlist when `sharp@^0.35.4` replaced `0.34.5`; a recurrence now fails the
> gate. See `docs/phase10-sharp-remediation.md`.

### Trivy ignorefile (`.trivyignore`)

| CVE/GHSA | Package | Why allowed | Removal condition |
|----------|---------|------------|-------------------|
| `CVE-2026-14257`, `CVE-2026-69152` | brace-expansion 5.0.7 (**F-05**) | **not an application dependency** — vendored inside the npm CLI at `/usr/local/lib/node_modules/npm/node_modules/brace-expansion` (base image); runtime `node server.js` never executes it; absent from `/app` (verified on rebuilt image) | upstream `node:24-alpine` npm refresh (or dropping npm from the image) |
| `CVE-2026-69192` | ip-address 10.2.0 (**F-05**) | same base-image npm CLI vendoring; absent from `npm ls`, `package-lock.json`, and `/app` | upstream `node:24-alpine` npm refresh |
| `CVE-2026-45623`, `CVE-2026-73646` | postcss 8.4.31 (**F-02**) | hard-pinned by `next@15.5.25`; build-time CSS pipeline only; no server module references it at runtime; no attacker-controlled CSS | Next 16 major bump (no safe 15.x path) |
| `CVE-2026-73566` | tar 7.5.19 (**F-05**) | same base-image npm CLI vendoring; next/dist/compiled/tar (unversioned, un-flagged) unaffected | upstream `node:24-alpine` npm refresh |

> **F-03 sharp entries REMOVED (2026-09-11):** `GHSA-f88m-g3jw-g9cj` and
> `GHSA-rgj7-g3m4-5g8c` were retired from `.trivyignore` when `sharp@^0.35.4` replaced
> `0.34.5`; raw rescan of `forex-ai-web:sharp` confirms both are gone. A recurrence now
> fails the Trivy gate. See `docs/phase10-sharp-remediation.md`.

These mirror the existing severity assessment in `docs/dependency-security-audit.md`
(§8/§9). None of them are CRITICAL, and none are reachable from a runtime code path
of the deployed stack. **F-04 (CVE-2026-14456) and F-03 (sharp GHSA pair) are no
longer exceptions** — F-04 was resolved/removed on 2026-09-11 (see
`docs/phase10-base-image-security.md`) and F-03 was resolved/removed the same day
(see `docs/phase10-sharp-remediation.md`).

## 9. Verification evidence (2026-09-11)

All checks were run locally against this repository; the exact CI commands were
replayed where practical.

| Check | Command / method | Result |
|-------|------------------|--------|
| Workflow YAML valid | `python3 -c "import yaml; yaml.safe_load(open(<wf>))"` on all 3 workflows | 3/3 parse OK |
| pip-audit runtime gate | fresh venv + `pip install -e ./backend` + `pip-audit --skip-editable` | **0 known vulnerabilities**, exit 0 |
| npm audit gate (allowlist) | `npm audit --omit=dev --json` → `node .github/scripts/check-npm-audit.cjs` | exit 0; exactly the 4 documented postcss exceptions (sharp IDs removed with the F-03 fix) |
| npm audit gate (failure path) | injected a synthetic new advisory ID into the JSON | gate fails, exit 1 (as designed) |
| npm audit gate (clean state) | `{"vulnerabilities":{}}` | gate passes, exit 0 (as designed) |
| Trivy — backend image | exact `docker-build.yml` command with `.trivyignore` on `forex-ai-api:ci-local` | **0 findings** (debian 13.6 + python-pkg, both clean), exit 0 |
| Trivy — frontend image (F-04 verification) | same on `forex-ai-web:ci-f04` after base fix | **8 HIGH / 0 CRITICAL**, all `.trivyignore`-justified, exit 0; raw scan without ignorefile confirms CVE-2026-14456 gone |
| Trivy — frontend image (F-03 verification) | same on `forex-ai-web:sharp` after the sharp upgrade, with the post-F-03 `.trivyignore` | **6 HIGH / 0 CRITICAL** (postcss×2 = F-02, brace-expansion×2/ip-address/tar = F-05), exit 0; raw scan confirms sharp GHSA pair gone |
| Backend unit + coverage gate | `pytest tests/unit --cov=app --cov-report=term-missing` | **463 passed**, coverage gate exit 0 |
| Backend integration | `pytest tests/integration` (real PostgreSQL + Redis) | **102 passed**, exit 0 |
| Frontend Vitest | `npx vitest run` | **66 passed** (4 files) |
| ESLint | `npm run lint` (`eslint .`) | exit 0 |
| TypeScript | `npx tsc --noEmit` | exit 0 |
| Next.js production build | `npm run build` | exit 0, 6 routes |
| SAFE MODE | `/health/ready` on the running stack | `{"status":"ok","mode":"safe"}`; no application file touched, so SAFE MODE is unchanged by definition |
| Secrets in diff | `grep -rInE` over all modified workflow/script/ignore files | no credentials, keys, or material secrets; only the pre-existing CI test fixture value and a word comment |

## 10. What GitHub Actions will execute that local replay cannot

- **`actions/setup-python` + pip cache restore** — local runs reuse the project venv;
  CI re-installs from `pyproject.toml` behind its cache key.
- **`actions/setup-node` + npm cache restore** — same pattern for `npm ci`.
- **Trivy scan inside the workflow runners** — replay used identical local images,
  but the runner job mounts its own Docker socket against the Trivy image pinned by
  digest; behavior (exit code / threshold) is identical to the local replay since the
  same `aquasec/trivy@sha256:91bc…77ad` image is executed.
- **Integration job service containers** (`postgres:16-alpine`, `redis:7-alpine`
  with health checks) — locally covered by the compose stack; CI provisions
  equivalents in the job's `services:` block.
- **Path-triggered execution** of `docker-build.yml` on PRs touching the listed
  paths — not applicable locally.
- **pip-audit network queries** to the PyPI OSV index — the gate is intentionally
  live (fresh advisories are picked up), which is exactly the value of running it in
  CI on every commit/PR.

## 11. Remaining Phase 10 items / limitations

- **~~F-04 (OpenSSL base layer)~~ RESOLVED (2026-09-11)** — `frontend/Dockerfile` prod
  stage now upgrades the OpenSSL package family (`openssl/libcrypto3/libssl3 →
  3.5.8-r0`) at image-build time; `CVE-2026-14456` was removed from `.trivyignore` and
  the rescan is clean. See `docs/phase10-base-image-security.md`. If the floating
  `node:24-alpine` base is ever rebuilt to ship `3.5.8-r0` natively, the explicit
  `RUN apk add --no-cache --upgrade openssl libcrypto3 libssl3` in the Dockerfile can
  be removed (verified against a rescan).
- **~~F-03 (sharp)~~ RESOLVED (2026-09-11)** — `sharp@^0.35.4` installed as a direct
  dependency (in-range for `next@15.5.25`); both GHSA findings and both npm audit IDs
  cleared; `.trivyignore` and `check-npm-audit.cjs` sharp entries removed so a
  regression fails the gates. See `docs/phase10-sharp-remediation.md`.
- **F-07 (Python reproducibility)** — **RESOLVED (2026-09-12)**: hashed
  pip-tools locks (`requirements.lock` / `requirements-dev.lock`) are installed
  with `--require-hashes` in the Dockerfile, Makefile, and CI; the pip-audit
  runtime-closure venv now installs the locked closure. Full record:
  `docs/phase10-python-lockfile.md`.
- **No real CA / HSTS / Let's Encrypt** — explicitly out of scope until a production
  domain exists (per project decision). `tls-deployment.md` operator notes remain
  deferred.
- **No Phase 11 / git tag / release** — not started; this phase ends at the CI gate.
- **Future bumps** must also update both exception stores (`.trivyignore` and
  `check-npm-audit.cjs`) *and* the justifications quoted here — the gates will fail
  loudly if an allow-listed finding disappears (passing) or a new one appears
  (failing), which is the intended contract.