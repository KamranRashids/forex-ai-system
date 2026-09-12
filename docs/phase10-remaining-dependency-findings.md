# Phase 10 — Remaining Dependency Findings

**Date:** 2026-09-11 · **Scope:** verify-only for F-05 (ip-address), F-02 (postcss),
F-03 (sharp). When this document was written no dependency changes had been applied;
F-03 was **subsequently remediated** the same day (see §3 below and
`docs/phase10-sharp-remediation.md`).

## 1. F-05 ip-address

**Status: ACCEPTED EXCEPTION (attribution corrected — not an application finding)**

| Attribute | Value |
|-----------|-------|
| Advisory | CVE-2026-69192 — ip-address: inconsistent IP address parsing leading to bypass |
| Trivy version reported | `ip-address@10.2.0`, severity HIGH |
| **PkgPath (Trivy JSON)** | `usr/local/lib/node_modules/npm/node_modules/ip-address/package.json` |
| In `npm ls` | **absent** (`forex-ai-frontend └── (empty)`) |
| In `package-lock.json` | **absent** (zero `ip-address` entries) |
| On the prod image filesystem `/app` | **absent** |
| Where it actually lives | **`/usr/local/lib/node_modules/npm/`** — the npm CLI shipped inside the `node:24-alpine` base image (npm v11.19.0) |

**Determination — not a stale layer, not a stale lockfile, not the app.** Trivy's
`PkgPath` is definitive: the package exists only inside the base image's bundled npm
CLI (`/usr/local/lib/node_modules/npm/node_modules/ip-address`), which is used by npm
itself (via `socks`). It is **not** a transitive dependency of the application —
`npm ls` and the lockfile prove it was never part of the app tree. **Same applies to
`brace-expansion@5.0.7` (CVE-2026-14257, CVE-2026-69152) and `tar@7.5.19`
(CVE-2026-73566)**, which share the identical `PkgPath` prefix and are npm CLI vendored
packages; the earlier audit's "dev tooling / stale layer" attribution was wrong.

**Fix options**
1. Upstream: when a future `node:24-alpine` rebuild bundles a newer npm, these
   disappear (no app action). Tracked automatically by the Trivy gate.
2. Image-level (reported, not applied): deleting `/usr/local/lib/node_modules/npm`
   (and `/usr/local/bin/npm`, `npx`) from the prod stage would remove these three
   findings; the container only runs `node server.js` so npm is never needed. Risky
   only if future debugging inside the container temporarily needs npm. Not applied
   because it is not necessary for current runtime safety.

## 2. F-02 postcss

**Status: ACCEPTED EXCEPTION**

| Attribute | Value |
|-----------|-------|
| Installed version | `postcss@8.4.31` (hard-pinned by `next@15.5.25`: `next` declares `"postcss": "8.4.31"` exactly) |
| PkgPath | `app/node_modules/next/node_modules/postcss/package.json` |
| Advisories (npm audit) | `1117015` GHSA-qx2v-qp2m-jg93 (moderate, XSS in CSS stringify), `1124252` GHSA-6g55-p6wh-862q (high, arbitrary file read via sourceMappingURL), `1130709` GHSA-fxqj-rqcc-2cmp (moderate, incomplete fix), `1139510` GHSA-r28c-9q8g-f849 (high, path traversal in source-map auto-loading) — also surfaced by Trivy as CVE-2026-45623, CVE-2026-73646 |
| In final runtime image | **present** (under `next/node_modules`) — but **no file under `/app/.next/server` references postcss** (CSS is compiled into static output at build time) |
| Tailwind copy | `postcss@8.5.26` (root, v4 tooling) is **already patched** and **not** in the image |

**Determination — build-time only, not runtime-reachable.** PostCSS is invoked by
Next's **build** pipeline to emit CSS bundles; the standalone server ships the compiled
static CSS and never parses stylesheets at request time. Even at build time it only
processes the repo's own `postcss.config.mjs` + CSS sources — there is **no
attacker-controlled CSS input anywhere**, so the sourceMappingURL / path-traversal
vectors (which require processing attacker-crafted CSS with a hostile sourcemap) are
not reachable by any actor. Do not claim "not exploitable because not imported" — the
precise reason is: the vulnerable parser is only ever fed repository-trusted CSS at
build time, never served bytes.

**Fix path** — Next pins `"postcss": "8.4.31"` exactly; no `next@15` release raises it
(we are on the newest 15.x = 15.5.25). Only a **Next 16 major** bump moves postcss to
a fixed version. A forced npm `overrides` on the nested copy would fight Next's hard
pin and could break the CSS pipeline — **not recommended as a minimal safe change**.

## 3. F-03 sharp

**Status: RESOLVED (2026-09-11 — upgraded to `sharp@^0.35.4`; see
`docs/phase10-sharp-remediation.md`)**

| Attribute | Value |
|-----------|-------|
| Installed version (pre-remediation) | `sharp@0.34.5` (Next's optional dependency, resolved within `"^0.34.3 \|\| ^0.35.4"`) |
| Installed version (now) | `sharp@0.35.4` — direct dependency (`frontend/package.json`: `"sharp": "^0.35.4"`); `next@15.5.25 → sharp@0.35.4` deduped |
| PkgPath (pre-remediation) | `app/node_modules/sharp/package.json` (+ native `@img/sharp-linuxmusl-x64` bindings) |
| Advisories (pre-remediation, both now clear) | GHSA-f88m-g3jw-g9cj (high, libvips: CVE-2026-33327, CVE-2026-33328, CVE-2026-35590, CVE-2026-35591; fix `<0.35.0`), GHSA-rgj7-g3m4-5g8c (high, libheif: GHSA-g89c-p67h-r497, GHSA-2jg2-4ch7-h545; fix `<0.35.4`) |
| In final runtime image | still **present** (lazy-loaded by `next/dist/server/image-optimizer.js:208`) — now the **fixed** `0.35.4` |
| App usage | **zero** — `grep next/image / <Image / <img / blurDataURL / loader` in `frontend/src` → no matches; `/public` contains only `.gitkeep` (no images) |

**Determination (original verify-only analysis) — shipped but not reachable by
application traffic:** the `/_next/image` optimizer route is live (any request returns
the optimizer's 400 `"The requested resource isn't a valid image."`), Next's default
config is **same-origin-only** (external URLs rejected), and with an empty `/public`
there is no valid image on the origin for the optimizer to decode — so the sharp decode
path can never be fed **attacker-controlled image content**. That reasoning justified a
low replication risk, but the fix was minimal and in-range, so it was applied: the
package was unpinned from Next's optional `^0.34.3 || ^0.35.4` by adding it as a direct
dependency pinned to the already-supported `^0.35.4`. Raw Trivy and `npm audit` confirm
both GHSA findings and both npm advisory IDs are gone.

## 4. Evidence

Repository and image evidence for every claim above:

| Claim | Evidence |
|-------|----------|
| ip-address/brace-expansion/tar live in base-image npm CLI | Trivy JSON `PkgPath = usr/local/lib/node_modules/npm/node_modules/...`; `docker run ... node -p require(.../npm/package.json).version` → npm `11.19.0`; per-package versions 5.0.7 / 10.2.0 / 7.5.19 confirmed |
| They are absent from `/app` | `find` + `grep -rl 'ip-address|brace-expansion' /app` → no `/app` paths; `npm ls ip-address` → `(empty)`; `package-lock.json` → zero entries |
| postcss ships in image | `find ... -name postcss` → `/app/node_modules/next/node_modules/postcss`; version `8.4.31` |
| postcss not referenced by server | `grep -rl postcss /app/.next/server` → **no matches** |
| sharp now fixed in image | `/app/node_modules/sharp`, version `0.35.4`, native `@img/sharp-libvips-linuxmusl-x64` + `@img/sharp-linuxmusl-x64` present (post-remediation rebuild) |
| sharp load path | `next/dist/server/image-optimizer.js:208: _sharp = require('sharp');` |
| optimizer live but inert | ran `forex-ai-web:verify` on :3003; `GET /` → 200; every `/_next/image?url=...` (incl. valid 1×1 PNG placed in `/app/public`) → **400** "not a valid image"; external URL → 400 |
| no next/image usage | `grep -rnE 'next/image|<Image|blurDataURL|loader' frontend/src` → no matches; `ls frontend/public` → `.gitkeep` only |
| next pins postcss and allows sharp 0.35.x | `npm view next@15.5.25 dependencies.postcss` → `8.4.31`; `npm view next@15.5.25 optionalDependencies` → `"sharp": "^0.34.3 \|\| ^0.35.4"` |
| sharp@0.35.4 compatible | `npm view sharp@0.35.4 engines` → `node >=20.9.0`; `dist-tags.latest` = `0.35.4` |

## 5. Runtime Reachability

| Package | Physical in image | Executed at runtime | Reaches vulnerable parser with attacker data |
|---------|-------------------|---------------------|---------------------------------------------|
| postcss 8.4.31 | Yes (`next/node_modules`) | No (no server module requires it; CSS pre-compiled) | **No** — never parses runtime or user data |
| sharp 0.35.4 (fixed) | Yes (`/app/node_modules/sharp`) | Lazy — only if `/_next/image` successfully encodes | **No** — fixed version; same-origin-only optimizer + zero valid images on origin; every probe 400 |
| ip-address 10.2.0 | Yes (`/usr/local/lib/node_modules/npm`) | No (npm CLI never invoked) | **No** — not on any code path of `node server.js` |
| brace-expansion 5.0.7 | Yes (npm CLI) | No | **No** |
| tar 7.5.19 | Yes (npm CLI) | No | **No** |

(`next/dist/compiled/tar` — Next's unversioned vendored tar — is present at
`/app/node_modules/next/dist/compiled/tar` but carries no version metadata and is not
flagged by Trivy; the flagged tar is the npm-CLI copy only.)

## 6. Remediation Decision

| Finding | Decision | Rationale |
|---------|----------|-----------|
| F-02 postcss 8.4.31 | **ACCEPTED EXCEPTION** | Build-time only; no attacker-controlled CSS; no safe minimal fix (next hard-pins; major-only path) |
| F-03 sharp 0.34.5 | **RESOLVED** (2026-09-11) | Shipped but unreachable at runtime (verified); minimal in-range fix `npm install sharp@^0.35.4` applied as a direct dependency — both GHSA findings and both npm audit IDs cleared. Full record in `docs/phase10-sharp-remediation.md` |
| F-05 npm-CLI vendored (ip-address/brace-expansion/tar) | **ACCEPTED EXCEPTION** | Not application dependencies; never executed at runtime; no app-level fix (base-image npm refresh); optional image-level npm removal documented (§1) |

No dependency metadata files were changed in this phase.

## 7. Trivy Results

Pinned `aquasec/trivy@sha256:91bccc...` (0.61.1), CI policy `--exit-code 1
--severity HIGH,CRITICAL --ignore-unfixed`, on freshly built prod images
(`docker build --target prod -t forex-ai-web:<tag> ./frontend`).

| Image | Raw scan (no ignorefile) | With `.trivyignore` |
|-------|---------------------------|----------------------|
| `forex-ai-web:verify` (pre-remediation) | **8 HIGH / 0 CRITICAL**: brace-expansion (CVE-2026-14257, CVE-2026-69152), ip-address (CVE-2026-69192), postcss (CVE-2026-45623, CVE-2026-73646), sharp (GHSA-f88m-g3jw-g9cj, GHSA-rgj7-g3m4-5g8c), tar (CVE-2026-73566) | **exit 0** (all 8 were the verified exceptions) |
| `forex-ai-web:sharp` (post-remediation) | **6 HIGH / 0 CRITICAL**: brace-expansion (CVE-2026-14257, CVE-2026-69152), ip-address (CVE-2026-69192), postcss (CVE-2026-45623, CVE-2026-73646), tar (CVE-2026-73566) — **sharp gone** | **exit 0** (all 6 = documented F-02/F-05 exceptions) |
| `forex-ai-api` (fresh) | 0 findings | exit 0 |

`js-yaml` no longer appears on any prod Trivy scan (original audit row was from a
dev-target/pre-`.dockerignore` image).

## 8. npm Results

- `npm ls ip-address` → `(empty)` — never an app dependency. Same for the other F-05
  packages (they are not in the app tree at all, which is why `npm audit` does not flag
  them: `npm audit` operates on the project closure, not the base image).
- `npm ls postcss` → `next@15.5.25 → postcss@8.4.31` (nested, vulnerable) and
  root `postcss@8.5.26` (deduped for `@tailwindcss/postcss@4.3.3` / `vitest` → `vite`,
  **patched**, not shipped).
- `npm ls sharp` → `sharp@0.35.4` (root, direct) and `next@15.5.25 → sharp@0.35.4`
  (deduped) — post-remediation.
- `npm audit --omit=dev --json` (pre-remediation) → 3 packages flagged (`next`
  moderate via postcss, `postcss`, `sharp`), **0 critical**; npm audit allowlist gate
  passes on the 6 documented exception IDs
  (`1117015, 1124252, 1130709, 1139510, 1124066, 1193725`).
- `npm audit --omit=dev --json` (post-remediation) → **2** packages flagged (`next`
  moderate via postcss, `postcss`), **0 critical** — **sharp gone**; allowlist gate now
  covers only the 4 postcss IDs (sharp IDs removed from
  `.github/scripts/check-npm-audit.cjs` so a regression fails the gate).

## 9. Verification

Everything below run against the current repository and the freshly built
`forex-ai-web:verify` (current Dockerfile incl. F-04 OpenSSL fix):

| Check | Result |
|-------|--------|
| Prod image build (`docker build --target prod`) | exit 0 |
| `forex-ai-web:verify` boot (`node server.js`, then `GET /`) | HTTP 200; Next.js 15.5.25 "Ready" |
| `/_next/image` probes (local + external + valid PNG) | all HTTP 400 — optimizer live, decode path never reached |
| Vitest | **66 passed** (4 files) |
| ESLint | exit 0 |
| TypeScript (`tsc --noEmit`) | exit 0 |
| Next.js production build | exit 0 (6 routes) |
| Backend unit + coverage gate | **463 passed**, 92.61% coverage ≥ 90% gate |
| Backend integration | **102 passed** |
| npm audit allowlist gate (CI parity) | exit 0 — exceptions only |
| Trivy gate (with `.trivyignore`) | exit 0 — exception-only (raw scan doc'd in §7) |
| OpenSSL in verify image | `libcrypto3`/`libssl3` 3.5.8-r0 (F-04 unaffected) |
| Runtime closure (`npm audit --omit=dev`) | 0 critical; only postcss/sharp (+ next via postcss) |
| Dev stack after verification | healthy (`make dev` stack — api, web, 5 workers, postgres, redis, prometheus, grafana all Up) |

No application, configuration, Dockerfile, or dependency files were modified by this
phase. Only documentation (this file, plus status updates in
`docs/dependency-security-audit.md` and `docs/phase10-ci-security.md`) and the
`.trivyignore` comments were updated.

## 10. Remaining Exceptions

| ID | CVE/GHSA / npm ID | Package | Class | Reason | Removal condition |
|----|-------------------|---------|-------|--------|-------------------|
| F-02 | CVE-2026-45623, CVE-2026-73646 / 1117015, 1124252, 1130709, 1139510 | postcss 8.4.31 | node-pkg (image) + npm audit | build-time only, no user CSS | Next major bump |
| F-05 | CVE-2026-14257, CVE-2026-69152 | brace-expansion 5.0.7 | node-pkg (base-image npm CLI) | not an app dep; not executed | `node:24-alpine` npm refresh / drop npm from image |
| F-05 | CVE-2026-69192 | ip-address 10.2.0 | node-pkg (base-image npm CLI) | not an app dep; not executed | same |
| F-05 | CVE-2026-73566 | tar 7.5.19 | node-pkg (base-image npm CLI) | not an app dep; not executed | same |

All exceptions are traced in the CI gates (`docker-build.yml` → `.trivyignore`,
`frontend-ci.yml` → `check-npm-audit.cjs`). **0 CRITICAL** throughout.

## 11. Phase 10 Status

| Item | Status |
|------|--------|
| F-01 next CRITICAL | **RESOLVED** |
| F-02 postcss | **ACCEPTED EXCEPTION** (verified build-time only; no safe minimal fix) |
| F-03 sharp | **RESOLVED** (see `docs/phase10-sharp-remediation.md`) |
| F-04 OpenSSL | **RESOLVED** |
| F-05 npm-CLI vendored | **ACCEPTED EXCEPTION** (attribution corrected: base-image npm CLI, not app dep) |
| F-06 pytest dev-only | PENDING (optional dev bump) |
| F-07 Python lockfile | **RESOLVED** (hashed pip-tools locks `requirements.lock`/`requirements-dev.lock`, `--require-hashes` wired into Docker/Makefile/CI — see `docs/phase10-python-lockfile.md`) |
| F-08 CI hardening | **RESOLVED** (see `docs/phase10-ci-security.md`) |
| F-09 Dependabot image coverage | PARTIAL — documented gap |
| Real CA / HSTS | NOT STARTED (deferred until a production domain exists, per project decision) |
| Phase 11 / tag / release | NOT STARTED |

F-03 was closed by `npm install sharp@^0.35.4` (single in-range, Next-declared
dependency) plus the full re-verification suite and docs
(`docs/phase10-sharp-remediation.md`). F-05/F-02 remain documented exceptions that
require only an upstream base-image refresh or a Next 16 major, which are out of phase
scope.