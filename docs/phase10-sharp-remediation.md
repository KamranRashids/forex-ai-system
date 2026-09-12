# Phase 10 — Sharp Remediation (F-03)

**Date:** 2026-09-11 · **Scope:** F-03 only (sharp libvips/libheif HIGH)

## 1. Finding

| Attribute | Value |
|-----------|-------|
| Advisory | **GHSA-f88m-g3jw-g9cj** (high) — libvips: CVE-2026-33327, CVE-2026-33328, CVE-2026-35590, CVE-2026-35591 (fix `<0.35.0`); **GHSA-rgj7-g3m4-5g8c** (high) — libheif: GHSA-g89c-p67h-r497, GHSA-2jg2-4ch7-h545 (fix `<0.35.4`) |
| npm advisory IDs | `1124066` (libvips), `1193725` (libheif) |
| Audit IDs | F-03 (HIGH) in `docs/dependency-security-audit.md`; suppressed in `.trivyignore` and `docs/phase10-ci-security.md` §8 |
| Affected package | `sharp` (Node.js image-processing; decompresses via libvips/libheif) |
| Fix range | `>= 0.35.4` |

The finding was real: `sharp@0.34.5` shipped in the prod `forex-ai-web` image under
`/app/node_modules/sharp`, pullable by Next's image optimizer
(`next/dist/server/image-optimizer.js:208`) — but this deploy has no `next/image` /
`<Image>` / `<img>` usage and an empty `/public`, so no attacker-controlled image can
reach the vulnerable parser (every `/_next/image` probe returns 400). Low
reachability, but the fix is minimal and in-range, so this phase applied it.

## 2. Previous Version

| Attribute | Value |
|-----------|-------|
| Version | `sharp@0.34.5` |
| Role | **transitive** — Next's optional dependency, resolved by npm within `"^0.34.3 \|\| ^0.35.4"` (declared `optional: true` in `package-lock.json`) |
| Resolved by | `next@15.5.25 optionalDependencies` |
| PkgPath (Trivy) | `app/node_modules/sharp/package.json` + native `@img/sharp-linuxmusl-x64` bindings |
| Advisories reported | 2 GHSA (above) on Trivy raw scan; 2 npm IDs in `npm audit --omit=dev` |

## 3. New Version

| Attribute | Value |
|-----------|-------|
| Version | `sharp@0.35.4` (`npm view sharp dist-tags.latest` = `0.35.4`) |
| Role | **direct** — `frontend/package.json` dependencies: `"sharp": "^0.35.4"` |
| Resolved in tree | root `node_modules/sharp@0.35.4`, **deduped** by `next@15.5.25 → sharp@0.35.4` (`npm ls sharp` shows both paths on one physical install) |
| Native bindings | `@img/sharp-libvips-linuxmusl-x64` + `@img/sharp-linuxmusl-x64` (musl, matches Alpine base) |
| Advisories reported | **none** — both GHSA and both npm IDs clear |

## 4. Compatibility

- `next@15.5.25` (newest 15.x) declares `optionalDependencies: { "sharp": "^0.34.3 || ^0.35.4" }`
  — `0.35.4` is **inside the declared supported range** (verified with `npm view`).
- `sharp@0.35.4` engines: `node >=20.9.0` — runtime image is **Node 24**
  (`node:24-alpine`), satisfied.
- No engine/strict-engine surprises: `npm install` completed with only an
  informational warning (`npm warn allow-scripts ... unrs-resolver@1.12.2 postinstall
  not covered by allowScripts`) — harmless, unrelated to sharp.
- **Next.js was not upgraded** (still `^15.5.25`); no `overrides`, no `--force`, no
  broad `npm upgrade`.

## 5. Package/Lockfile Changes

Applied with the existing npm/package-lock workflow in `frontend/`:
`npm install sharp@^0.35.4 --no-audit --no-fund` → **"changed 3 packages in 10s"**.

| File | Change |
|------|--------|
| `frontend/package.json` | `dependencies` gains `"sharp": "^0.35.4"` (only intended dependency change; no other dep moved) |
| `frontend/package-lock.json` | `node_modules/sharp` `0.34.5 → 0.35.4` (was flagged `"optional": true`); `@img/*` packages refreshed; lockfile re-synced |

Post-install tree sanity: `npm ls` → clean, no invalid/extraneous/UNMET entries;
`npm ls sharp` → `sharp@0.35.4` (root) + `next@15.5.25 → sharp@0.35.4` deduped.

## 6. npm Audit Results

`npm audit --omit=dev --json` (production closure, the same scope as the CI gate):

| Metric | Before | After |
|--------|--------|-------|
| Packages flagged | 3 (`next` via postcss [moderate], `postcss`, **`sharp`**) | **2** (`next` via postcss [moderate], `postcss`) — **sharp gone** |
| Severities | {moderate: 1, high: 2, critical: 0} | {moderate: 1, high: 1, critical: 0} |
| Gate (`.github/scripts/check-npm-audit.cjs`) | exit 0 (6 allowed IDs incl. sharp) | exit 0 (4 allowed postcss IDs only — sharp IDs `1124066`/`1193725` removed so a regressing sharp advisory now **fails** the gate) |

## 7. Trivy Results

Pinned `aquasec/trivy@sha256:91bcccab7cb503127b0d792db7c652c4e556c9ff30eeb0908b6ae1980d7727ad`
(0.61.1), CI policy `--exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed`, image
`forex-ai-web:sharp` (`docker build --target prod -t forex-ai-web:sharp ./frontend`):

| Scan | Before (web:verify) | After (web:sharp) |
|------|---------------------|-------------------|
| Raw (no ignorefile) | 8 HIGH / 0 CRITICAL — incl. sharp GHSA-f88m-g3jw-g9cj, GHSA-rgj7-g3m4-5g8c | **6 HIGH / 0 CRITICAL — sharp advisories ABSENT**; remaining: brace-expansion×2 (F-05), ip-address (F-05), postcss×2 (F-02), tar (F-05) |
| With `.trivyignore` (post-removal, CI parity) | exit 0 | **exit 0** |

`.trivyignore` F-03 entries (`GHSA-f88m-g3jw-g9cj`, `GHSA-rgj7-g3m4-5g8c`) were
**removed**; the file now carries only F-02 + F-05 exceptions (plus RESOLVED comment
markers for F-04 and F-03). Verified no new HIGH/CRITICAL appeared.

## 8. Runtime Image Verification

| Check | Result |
|-------|--------|
| `docker build --target prod -t forex-ai-web:sharp ./frontend` | exit 0 |
| sharp in image | `0.35.4`; native `@img/sharp-libvips-linuxmusl-x64` + `@img/sharp-linuxmusl-x64` present |
| Prod overlay (`make prod-up`) | 12 services up — 11 healthy + nginx running |
| Host listening ports | **only `:80` and `:443`** (nginx) — no 5432/6379/8000/3000/9090/3001 |
| Edge probes | `/healthz`, `/health/live`, `/health/ready` → `{"status":"ok","mode":"safe"}`; `/` (frontend) → HTTP 200 |

## 9. Application Tests

| Check | Result |
|-------|--------|
| Vitest (`npx vitest run`) | **66 passed** (4 files) |
| ESLint (`npm run lint`) | exit 0 |
| TypeScript (`npx tsc --noEmit`) | exit 0 |
| Next.js production build (`npm run build`) | exit 0 (6 routes) |
| npm audit allowlist gate | exit 0 |
| Trivy gate (with updated `.trivyignore`) | exit 0 |

No application, test, Dockerfile, compose, nginx/TLS, or trading code was changed —
the sharp upgrade is an npm dependency/lockfile change only.

## 10. SAFE MODE Verification

Prod overlay live over the HTTPS edge:

- `GET /health/ready` → `{"status":"ok","mode":"safe"}`
- `GET /system/status` → `app_env: prod`, **`trading_mode: safe`**, **`safe_mode: true`**
- SAFE MODE is enforced by the unchanged backend application; a frontend dependency
  bump cannot alter it. Re-verified live.

## 11. TLS/WSS Verification

| Check | Result |
|-------|--------|
| HTTP → HTTPS | `http://localhost/` → **301** `Location: https://localhost/` |
| HTTPS document | **HTTP/2 200**, `x-frame-options: DENY`, `x-content-type-options: nosniff`, full CSP (`default-src 'self'` …) |
| TLS handshake | TLSv1.3 `TLS_AES_256_GCM_SHA384`, self-signed `subject=CN=localhost` (staging cert) |
| API over HTTPS | register 201 / login 200 / `ws/ticket` 200 / `auth/me` 200 (real one-time ticket) |
| WSS | `wss://localhost/api/v1/ws/stream?ticket=…` → `{"type":"subscribed","topics":["alerts"]}` ack within 5s |

## 12. Remaining Exceptions

**0 CRITICAL** overall. Remaining HIGH findings (all justified, all on
`.trivyignore` / postcss allowlist — untouchable by a minimal safe change):

| ID | Advisory | Package | Why it stays |
|----|----------|---------|--------------|
| F-02 | CVE-2026-45623, CVE-2026-73646 / npm 1117015, 1124252, 1130709, 1139510 | postcss 8.4.31 (next-pinned) | Next 15 hard-pins `"postcss": "8.4.31"`; build-time CSS only; removal requires a Next 16 major |
| F-05 | CVE-2026-14257, CVE-2026-69152 | brace-expansion 5.0.7 | npm-CLI vendored in `node:24-alpine` base; not an app dep, never executed |
| F-05 | CVE-2026-69192 | ip-address 10.2.0 | same base-image npm CLI vendoring |
| F-05 | CVE-2026-73566 | tar 7.5.19 | same base-image npm CLI vendoring |

Sharp is **no longer an exception anywhere** (`.trivyignore`, npm audit allowlist,
docs).

## 13. Rollback

- Revert `frontend/package.json` (drop `"sharp"`), then `npm install` / `npm ci` to
  restore `package-lock.json` — sharp returns to Next's optional resolution (0.34.5
  or whatever `"^0.34.3 || ^0.35.4"` then yields).
- Rebuild the prod image and re-run the app suite + Trivy.
- If the revert reintroduces the advisories, re-add ONLY the documented F-03
  entries to `.trivyignore` (`GHSA-f88m-g3jw-g9cj`, `GHSA-rgj7-g3m4-5g8c`) and the
  npm audit allowlist (`1124066`, `1193725`) **with the original justification**, and
  flip F-03 back to ACCEPTED EXCEPTION in the docs.
- No database, volume, or state migration is involved; no risk to trading data.

## 14. Acceptance Checklist

| Requirement | Status |
|-------------|--------|
| `sharp` upgraded to `^0.35.4` (≥ 0.35.4) as a direct dependency | COMPLETE |
| Next.js **not** upgraded; no overrides / `--force` / broad npm upgrade | COMPLETE |
| No application, trading, backend, Docker, Nginx/TLS/HSTS/CA, or SAFE MODE change | COMPLETE |
| package.json + package-lock.json the only dependency files changed | COMPLETE |
| `npm ls sharp` verifies 0.35.4 (deduped against next) | COMPLETE |
| `npm audit --omit=dev` before/after recorded; sharp gone | COMPLETE |
| npm audit allowlist gate passes with sharp IDs removed | COMPLETE |
| Prod image built via project procedure (`docker build --target prod`) | COMPLETE |
| Trivy raw rescan — sharp GHSA pair absent; 6 HIGH / 0 CRITICAL | COMPLETE |
| Trivy CI gate (with updated `.trivyignore`) exit 0 | COMPLETE |
| `.trivyignore` sharp entries removed; unrelated exceptions preserved | COMPLETE |
| Vitest / ESLint / tsc / Next build all green | COMPLETE |
| Prod overlay up; 12 services healthy; `/health/ready` + `/system/status` OK | COMPLETE |
| `trading_mode=safe`, `safe_mode=true` | COMPLETE |
| Staging TLS (301 → HTTPS, HTTP/2, self-signed CN=localhost) + WSS subscribe | COMPLETE |
| No unintended host ports (only nginx 80/443) | COMPLETE |
| Git diff limited to intended files | COMPLETE |