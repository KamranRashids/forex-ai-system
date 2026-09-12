# Phase 10 — Security Hardening: Nginx headers/CSP + per-client-IP rate limiting

Date: 2026-09-11. Status: **DONE (verified live)**. Component: `infra/nginx/nginx.conf`,
`backend/app/core/config.py`, `backend/app/api/deps.py`, `frontend/Dockerfile`,
`docker-compose.prod.yml`, tests.

## Scope of this step

1. **Nginx edge security headers + Content-Security-Policy.**
2. **Per-client-IP rate limiting behind Nginx** — the backend must use the original
   client IP (recovered via trusted forwarded headers from Nginx only) for its auth
   rate-limit keys, while remaining spoof-immune.

Explicitly deferred (next Phase 10 items, not done here): TLS/HSTS on Nginx, prod
resource limits, backup/restore, CI audit gates, release/sign-off.

## Changes made

### 1. Trusted-proxy client-IP recovery (backend)

- `backend/app/core/config.py`
  - New setting `trusted_proxies: str = ""` and property `trusted_proxy_cidrs`
    → `list[IPv4Network|IPv6Network]`. Empty string = no trusted peers (default:
    dev/direct mode keeps the old peer-IP behaviour).
- `backend/app/api/deps.py`
  - `client_ip(request, settings=None)` now:
    - uses the supplied settings (or `get_settings()`) to resolve trusted CIDRs;
    - honours forwarded headers (`X-Real-IP` preferred, right-most `X-Forwarded-For`
      entry as fallback) **only when the immediate socket peer is inside**
      `TRUSTED_PROXIES`;
    - otherwise returns the raw socket peer IP and ignores the headers;
    - returns `None` only if the peer is genuinely absent.
  - `enforce_rate_limit` passes `settings` through so requester path stays unchanged.
  - All existing single-arg callers (`auth.py`, `alerts.py:82`, `admin.py`,
    `risk.py:84`, `users.py:72`) are untouched.

### 2. Nginx edge (`infra/nginx/nginx.conf`)

- **CSP** (project-derived, see “Security rationale” below):
  `default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none';
  frame-src 'none'; object-src 'none'; img-src 'self' data:; font-src 'self' data:;
  style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline';
  connect-src 'self'; worker-src 'self'` — plus `always` so 4xx/5xx carry it too.
- **Other headers** (kept + added): `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`,
  `Permissions-Policy: camera=(), geolocation=(), microphone=(), payment=(), usb=()`;
  `server_tokens off`.
- **Forwarded header hygiene**: every backend-facing location sets
  `X-Real-IP: $remote_addr` and `X-Forwarded-For: $proxy_add_x_forwarded_for`
  (which appends, so inbound client values can never leak to the backend as “trusted”).
- **Bug fixes discovered during verification**
  - `location /api/ { proxy_pass http://api_upstream/; }` STRIPPED the `/api/` prefix
    (trailing-slash URIs are replaced) → `/api/v1/*` was a 404 through Nginx. Fixed by
    removing the trailing slash (full URI preserved).
  - The `Upgrade`/`Connection` WebSocket headers existed only on `/ws/`, but live
    streams actually proxy via `/api/v1/ws/stream` → WS handshake 404 through Nginx.
    Upgrade headers + `proxy_read_timeout 3600s` added to `/api/` too.
  - API-owned root probe paths (`/health/live`, `/health/ready`, `/system/status`)
    were not routed at the edge (they hit the Next app → HTML 404). Added exact-match
    locations proxying them to `api_upstream` (keeps `SAFE MODE` visible through Nginx
    and makes the frontend health check work in prod).
- Note: TLS-termination comment retains the planned Phase 10 listen-443 block.

### 3. Prod build/deploy wiring

- `frontend/Dockerfile` (builder stage): `ARG NEXT_PUBLIC_API_URL=` /
  `ARG NEXT_PUBLIC_WS_URL=` (default empty) yielded to `ENV` → prod image bakes
  **same-origin** API/WS endpoints, which is what makes `connect-src 'self'` valid.
- `docker-compose.prod.yml`
  - api env: `TRUSTED_PROXIES: "172.28.0.0/16"`.
  - web service: `build.args.NEXT_PUBLIC_API_URL: ""`, `NEXT_PUBLIC_WS_URL: ""`.
  - default network pinned via `networks.default.ipam.subnet: 172.28.0.0/16` so the
    trusted range covers only the stack’s own containers.

## Security rationale (CSP)

CSP was derived from the **actual** resources the app loads, not a guess:

- No external fonts/images/scripts anywhere (verified by scanning the served HTML and
  JS chunks of the prod build — the only non-local URLs are library-internal literals
  such as core-js/React/Next error-docs links and URL-parser test strings).
- Next.js App Router streams its RSC hydration payload via inline `<script>`/`<style>`
  tags, and `backtests/page.tsx` uses one inline `style={{...}}` attribute. This is why
  `script-src`/`style-src` include `'unsafe-inline'` (genuine requirement of the current
  architecture, not a convenience).
- `'unsafe-eval'` is deliberately **not** allowed.
- `connect-src 'self'` covers the same-origin REST API (`/api/*`), WebSockets
  (`/api/v1/ws/stream` — CSP3 equates same-origin `ws:` with `'self'`), and health
  probes. The prod build bakes empty `NEXT_PUBLIC_API_URL`/`WS_URL` (verified: zero
  `localhost:8000` / `ws://localhost` references in any served chunk). Deployments that
  bake explicit endpoint hosts must extend `connect-src` accordingly (documented in the
  nginx.conf comment).

## Files changed

| File | Change |
|---|---|
| `backend/app/core/config.py` | `trusted_proxies` + `trusted_proxy_cidrs` |
| `backend/app/api/deps.py` | `client_ip` trust model; `enforce_rate_limit` passes settings |
| `infra/nginx/nginx.conf` | CSP + Permissions-Policy; `/api/` prefix fix + WS upgrade; forwarded-header hygiene; system-probe passthrough |
| `frontend/Dockerfile` | `NEXT_PUBLIC_API_URL`/`NEXT_PUBLIC_WS_URL` build args (empty default) |
| `docker-compose.prod.yml` | `TRUSTED_PROXIES`, subnet pin, web build args |
| `backend/tests/unit/test_client_ip.py` | 10 new unit tests |
| `backend/tests/integration/test_auth_flow.py` | spoofed-header 429 test |

## Verification (all run against the actual code)

### Static / unit (backend)
- `ruff check .` — exit 0.  `ruff format --check` on changed files — clean.
- `pytest tests/unit --cov=app` — **463 passed**, coverage **92.61%** (gate 90%).
- `pytest tests/integration` — **102 passed** (dev postgres/redis).
- Note: `ruff format --check .` flags a **pre-existing** baseline violation in
  `backend/tests/integration/test_market_api.py` (lines 87-98, 118-119, 144-145,
  163-164); that file was not modified by this step.

### Frontend (unchanged source, re-verified)
- `npm test` (vitest) — 66/66 passed.  `npm run lint` — clean.  `npx tsc --noEmit` — clean.

### Live through-Nginx verification (prod overlay, port 80)
Stack: `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`,
nginx `nginx -t` ok, default network subnet `172.28.0.0/16` confirmed.

| Check | Result |
|---|---|
| Security headers on `/` (CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, `Server: nginx`) | present |
| `GET /healthz`, `/health/ready`, `/system/status` through Nginx | 200, `trading_mode: safe` |
| `/api/v1/auth/register`, `/auth/login`, `/ws/ticket` through Nginx | 201 / 200 / 200 |
| WS `subscribe` ack over `ws://localhost/api/v1/ws/stream?ticket=…` through Nginx | subscribed |
| Login flood (14 wrong-password attempts) | 429 observed (rate limit enforced) |
| 14 spoofed attempts with varying `X-Real-IP`/`X-Forwarded-For` | all 429, **no escapes** → spoofing cannot bypass |
| Audit log `auth.user_registered` `ip_address` (via psql) | `172.28.0.1` — the client IP forwarded by Nginx (trust path proven) |
| Served HTML/JS chunks | no `localhost:8000`/`ws://localhost` baked → CSP `connect-src 'self'` compatible |

Dev stack restored afterwards; dev API `/system/status` re-verified `safe_mode: true`.

## Remaining Phase 10 gaps (NOT part of this step)

- **M1** Nginx TLS (443, HSTS, 80→443 redirect) + cert management.
- **M4** CI gates: `pip-audit`, `npm audit`, Trivy in CI.
- **M5** Prod resource limits in compose overlay.
- **M6** Backup/restore script + procedure + restore drill.
- **M7** Secrets rotation runbook + Grafana cred policy + `.env.example` reconciliation.
- **M8** Monitoring/alerting prod-access posture doc.
- Optional follow-ups documented: CSP via backend middleware (direct API access),
  Redis-backed rate limiting for multi-instance scale, WS connection caps.

## Acceptance checklist for this step

- [x] CSP + security headers proven on the edge (live curl).
- [x] CSP compatible with the actual frontend build (same-origin only; inline RSC
      scripts/style covered; no `unsafe-eval`).
- [x] Rate limiting keys on real client IP behind Nginx (audit log shows forwarded IP).
- [x] Spoof attempts through Nginx cannot bypass (all 429).
- [x] SAFE MODE unchanged (`mode=safe`), WS stream functional through Nginx.
- [x] Backend/frontend test, lint, type-check and coverage gates green.
- [x] Dev stack restored; docs updated (`threat-model.md`, `dependency-security-audit.md`).