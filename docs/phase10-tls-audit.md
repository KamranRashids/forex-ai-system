# Phase 10 — TLS/HSTS Audit

Date: 2026-09-11. Status: **AUDIT COMPLETE → Phase-1 STAGING TLS IMPLEMENTED.**

- This document was originally **AUDIT ONLY** (no files changed, no certs generated).
  It determined exactly how HTTPS/TLS + HSTS should be introduced into the existing
  architecture. Findings are classified **COMPLETE / PARTIAL / MISSING /
  NEEDS VERIFICATION**; that analysis remains below and is still the production plan.
- **Phase 1 (staging TLS, self-signed, HSTS OFF) is now DONE and verified**
  (2026-09-11) per §7/§12/§13/§15. Full evidence + acceptance checklist:
  **`docs/phase10-staging-tls.md`**. Key results: only `:443` + `:80` externally
  published (`ports: !override []` verified via `config`/`ps`/`ss`), HTTP → 301 →
  HTTPS, `wss://localhost` subscribe acked, `/health/ready` + `/system/status` =
  `safe`/`trading_mode=safe`, certs gitignored under `infra/certs/`.
- **Remaining (Phases 2–4):** real CA certificate for the real domain (§8 operator
  inputs), HSTS enablement (§10), `docs/tls-deployment.md`/runbook + scripts,
  M1 sign-off. HSTS remains deliberately absent until the CA phase.

---

## 1. Current TLS State

| Item | State | Classification |
|---|---|---|
| HTTPS anywhere | Nginx listens on plaintext `:80` only; `infra/nginx` contains only `nginx.conf` (no certs, no `listen 443`) | **MISSING** |
| HSTS header | Never set anywhere | **MISSING** |
| TLS termination point | None; edge is plaintext HTTP so all credentials/refresh cookies traverse the wire in the clear | **MISSING** |
| Backend Secure cookies | `secure=settings.cookie_secure` and `cookie_secure == (app_env == "prod")` → true in prod overlay (`auth.py:_set_refresh_cookie`) | **NEEDS VERIFICATION** — this already *requires* HTTPS for refresh-cookie auth to work in a browser in prod; there is no HTTPS yet, so prod cookie auth is effectively unusable until TLS lands |
| Access tokens (Bearer) | Delivered to the browser over HTTP today; `sessionStorage`-scoped (`frontend/src/lib/auth.ts`) | **MISSING** (transport) |
| App separation of API/WS from edge | All `/api/*`, `/ws/*`, health, and `/` proxied through nginx; frontend prod build bakes same-origin endpoints | **COMPLETE** |

**Conclusion:** TLS is entirely absent, yet a security control already in the code
(`cookie_secure` for prod) silently depends on it. TLS is the critical missing
transport control (threat-model §3 TB1, §21 Ms, scenario T7, M1).

---

## 2. Current Nginx State

`infra/nginx/nginx.conf` (single file, volume-mounted into `nginx:1.27-alpine` ro):

| Aspect | State | Classification |
|---|---|---|
| `listen` | `80` only; placeholder comment at line 41: "TLS termination … added in Phase 10" | **PARTIAL** (hook documented, not implemented) |
| `server_name` | `_` (catch-all default) — no real domain/SNI yet | **PARTIAL** |
| Security headers | `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, CSP (`always`) | **COMPLETE** |
| `server_tokens off` | set | **COMPLETE** |
| Header hygiene | every backend-facing location sets `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto $scheme` | **COMPLETE** — `$scheme` will automatically become `https` inside a 443 `server` block |
| WS upgrade | `/api/` and `/ws/` carry `Upgrade`/`Connection` + `proxy_read_timeout 3600s` | **COMPLETE** — works unchanged for `wss` (nginx terminates TLS, upstream stays `http://api_upstream`) |
| Health passthrough | `/healthz`, `/health/live`, `/health/ready`, `/system/status` proxied to api | **COMPLETE** |
| TLS-specific nginx directives | `ssl_certificate`, `ssl_certificate_key`, `listen 443 ssl`, `ssl_protocols`, `add_header Strict-Transport-Security`, HTTP→HTTPS redirect server | **MISSING** |

---

## 3. Current Production Port Exposure

Merged result of `docker compose -f docker-compose.yml -f docker-compose.prod.yml config`
(evidence gathered 2026-09-11): the produced config contains published-port entries
(`- mode: ingress`) for **seven** services:

| Service | Published (host) port | Prod should expose? |
|---|---|---|
| `nginx` | `${NGINX_HTTP_PORT:-80}:80` | Yes (redirect + LE HTTP-01) |
| `api` | `${API_PORT:-8000}:8000` | **No** — must stay behind nginx |
| `web` | `${FRONTEND_PORT:-3000}:3000` | **No** — must stay behind nginx |
| `postgres` | `${POSTGRES_PORT:-5432}:5432` | **No** — DB must not be externally reachable in prod |
| `redis` | `${REDIS_PORT:-6379}:6379` | **No** — redis must not be externally reachable in prod |
| `prometheus` | `${PROMETHEUS_PORT:-9090}:9090` | **No** (monitoring posture decision deferred to M8) |
| `grafana` | `${GRAFANA_PORT:-3001}:3000` | **No** (same; anonymous Viewer must not be public) |

### The `ports: []` limitation — investigated

The overlay's `api`/`web` services already contain `ports: []` (added during M3) and the
comment claims "no host DB/Redis ports". **This was not taking effect.** Compose-go
merges `ports` by *target key and appends*; an empty list in an override file does **not**
clear the base file's list. Confirmed by the resolved `config` output above (7 ingress
entries) and by the prior live prod run where `0.0.0.0:5432->5432` etc. were published.

**Correct fix (determined):** the Compose spec provides the `!override` YAML tag for
exactly this. In `docker-compose.prod.yml`, each service whose host exposure must be
removed declares:

```yaml
ports: !override []
```

(Docker Compose ≥ v2.24.0 / compose-go supports `!override`; the project runs v5.5.0.)
Resulting prod exposure: **only `nginx`** on `${NGINX_HTTPS_PORT:-443}:443` and
`${NGINX_HTTP_PORT:-80}:80` (redirect + HTTP-01). `postgres`, `redis`, `api`, `web`,
`prometheus`, `grafana` become **internal-network-only**. This matches the threat-model
posture (§10, §11, §21) and M1/§3 TB1.

Implication for health checks: Docker host-side probes that previously hit
`localhost:8000/health/live` must switch to the edge (`https://host/healthz`) — the
runbook's dev/localhost commands (§2–§3, e.g. `curl http://localhost:8000/system/status`)
already document the *dev* stack; prod probes go through nginx.

---

## 4. TLS Architecture

```
Internet ──443──> nginx (:443 ssl, certs from mounted volume) ──http──> api:8000 (FastAPI)
        │                                                   └──http──> web:3000  (Next standalone)
        │                                                     (internal compose network 172.28.0.0/16)
        └──80──> nginx (:80) ──301──> https://$host:443  (redirect + LE HTTP-01)
```

- **Termination at the edge (nginx)** — nginx owns certs; upstream services never touch
  TLS. Internal traffic stays plaintext within the pinned `172.28.0.0/16` subnet
  (acceptable: compose-internal, no forged peers; out of scope to encrypt laterally).
- **One cert hosting `server_name`** for the UI+API+WS (single domain; all origins are
  the same host). No wildcard/multi-SAN required unless subdomains are added.
- **Same-origin invariant preserved** — `NEXT_PUBLIC_API_URL`/`WS_URL` stay baked empty
  so the browser always talks `https://host/...` and `wss://host/...` through nginx;
  CSP `connect-src 'self'` remains valid (no CSP edits needed).
- `X-Forwarded-Proto` already propagates `https` to the API (trusted-proxy model from M3
  is unaffected).
- **Health 100% internal** — container healthchecks hit `http://127.0.0.1:8000/...`
  inside the api container directly (never cross the edge); they are TLS-independent.

### Dependency on existing controls (verified)
- **Refresh cookie requires HTTPS in prod**: `secure=settings.cookie_secure`
  (`auth.py:43`) with prod → `True`. Over HTTP the browser refuses to store/send the
  Secure cookie. TLS is what makes prod auth actually usable.
- **WSS Origin allowlist requires an https origin**: `ws/hub.py:origin_allowed()`
  compares the browser `Origin` against `cors_origins` (browsers send `Origin` even for
  same-origin WS). Therefore `CORS_ORIGINS` in the prod environment **must include**
  `https://<host>` or WSS is rejected (classification: **NEEDS VERIFICATION** — the exact
  origin value is an operator input; base default is `http://localhost:3000` which will
  fail over HTTPS). Note non-browser clients (no `Origin`) remain permitted — unchanged.

---

## 5. HTTP to HTTPS Strategy

Recommended: **keep port 80, but only redirect + ACME HTTP-01**.

- `server { listen 80; ... return 301 https://$host$request_uri; }` — minimal, no content
  served over HTTP. Keeps Let's Encrypt HTTP-01 challenge reachable and any human who
  types `http://` lands on `https://`.
  - Add `if ($request_uri != "/.well-known/acme-challenge/") { return 301 ...; }`-style
    splitting **only if** HTTP-01 is used; otherwise a bare redirect server is enough
    (DNS-01 needs no HTTP path).
- Alternative if the operator insists on zero HTTP: expose only 443 and use DNS-01 for
  issuance; document that `localhost`/LAN redirect convenience is lost. Both variants
  are captured below in §6 as deployment possibilities.
- WS: a browser never opens `ws://` because the frontend's baked `WS_URL` is same-origin
  (`""`) → always `wss://host/...`. No websocket redirect needed (and WS redirects are
  unreliable — avoided).

**Decision recorded:** redirect-on-80 is the default; full-block of HTTP is the
variation for closed networks using DNS-01.

---

## 6. Certificate Strategy

Two deployment possibilities are documented; **no certs are generated in this audit**.

### Possibility A — Local/staging/self-signed TLS (verification only)
- Purpose: validate the *whole* TLS path locally (handshake, redirect, WSS, HSTS wiring,
  headers, made-up port hygiene) before any public exposure or CA involvement.
- Method: generate a **private CA + server certificate** (e.g. `mkcert` for OS-trusted
  browser testing, or `openssl req -x509` for ops-style testing), SANs = `localhost` +
  any staging hostname/IP. Store under `infra/certs/` (gitignored; **never commit**).
- Mount: `./infra/certs:/etc/nginx/certs:ro`.
- **HSTS disabled in this mode** — a self-signed cert + HSTS would hard-pin the browser's
  HSTS cache for the hostname, bricking later testing. HSTS ships only with real certs.
- This is the Phase-verification pass (see §15) and must be reproducible in a disposable
  branch/config.

### Possibility B — Real production certificate management
- Certs issued by a public CA for the real domain, auto-renewed. Two clean options:
  1. **Let's Encrypt (HTTP-01)** via `certbot/certbot` container side-by-side with nginx
     (webroot or a standalone challenge volume), renew in a host cron/systemd timer or a
     sidecar container loop, reload nginx after renewal. Requires outbound port 80 from
     the CA to the host + an `A`/`AAAA` record.
  2. **DNS-01** (LE or commercial CA) when the provider has an API (e.g. Cloudflare/Acme
     DNS-challenge hook); avoids inbound 80 entirely and enables wildcards later.
     Requires DNS API credentials/automation policy (operator decision).
- Cert/key land at the same mounted paths used in Possibility A (`/etc/nginx/certs/…`,
  `fullchain.pem` + `privkey.pem`) so nginx config is CA-agnostic.
- Renewal = swap files + `nginx -s reload` (zero-downtime; nginx re-reads on reload).
- **No CA/DNS/domain assumed in this phase** — everything below is an explicit operator
  input.

---

## 7. Local/Staging Verification

(To run only after implementation — listed here as the plan, nothing executed yet.)

1. Generate private CA + host cert (SAN: `localhost`/staging name; `mkcert` preferred)
   into `infra/certs/`.
2. Apply the §12 compose + §13 nginx changes with a **staging-only** flag that keeps HSTS
   off (e.g. compose `HSTS_ENABLED=false`; nginx `map`/`if` on it, or a separate
   `nginx.https.conf` fragment).
3. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`.
4. Verify ONLY nginx ports are published (build a port-emission assertion):
   `docker compose ps` must show no `:5432`/`:6379`/`:8000`/`:3000`/`:9090`/`:3001`.
5. `curl -k https://localhost/` → 200, correct CSP/headers; `curl -sI http://localhost/`
   → `301 Location: https://localhost/…`.
6. WSS: open `wss://localhost/api/v1/ws/stream?ticket=…` (real ticket) → `subscribed`.
7. `/healthz`(https), `/health/ready`, `/system/status` → `safe`.
8. Browser smoke test (sessionStorage auth, refresh-cookie rotation over https).
9. Full backend + frontend suites (§15).

---

## 8. Production Certificate Requirements

Information/config the operator must supply **before real-cert TLS can be configured**
(no assumptions made about domain/CA/DNS):

| # | Input | Why | Default if omitted |
|---|---|---|---|
| 1 | **Domain name** (e.g. `fx.example.com`; affects `server_name` + `CORS_ORIGINS` + cert SANs) | nginx `server_name`, browser origin validation, cert CSR | staging/`localhost` (Possibility A only) |
| 2 | **DNS record** (`A`/`AAAA` ⊕ `host`) pointing at the host's public IP | so CA/http-01 and browsers resolve the host | n/a |
| 3 | **Public inbound policy**: is `443` (and `80` if HTTP-01) reachable from the internet, or is a closed network meant? | chooses Possibility B option 1 vs 2 | assume open 443/80 |
| 4 | **Certificate Authority choice** (Let's Encrypt vs commercial) + any account/registration email if LE | issuance + TOS acceptance | LE (chosen) — needs operator consent/email |
| 5 | **HTTP-01 vs DNS-01**; if DNS-01, which DNS provider + whether API automation is permitted (creds stay out of repo) | renewal automation path | HTTP-01 (default) |
| 6 | **Renewal cadence/mechanism** (host cron vs sidecar container) | ops ownership | host cron + `nginx -s reload` |
| 7 | **`CORS_ORIGINS`** prod value (must include `https://<domain>`) | WS/WSS origin allowlist + CORS | currently `http://localhost:3000` → **will break WSS over https** (NEEDS VERIFICATION) |
| 8 | **HSTS rollout decision** (max-age, includeSubDomains, preload opt-in) | policy | enabled 31536000s after phase 2 confirms HTTPS |
| 9 | Wildcard/multi-SAN need (subdomains later?) | SANs in CSR | single-domain SAN |

Secrets handling: private keys and CA material never enter the repo or `.env`; mounted
from `infra/certs/` (gitignored) or an external secret path. No secrets are printed in
this audit.

---

## 9. WebSocket/WSS Requirements

- Browser always uses `wss://<host>/api/v1/ws/stream?ticket=…` because `WS_URL` is baked
  to the same-origin value (`frontend/src/lib/config.ts` + prod build args) — **no
  frontend change required**.
- nginx `/api/` location already performs the `Upgrade` proxying to
  `http://api_upstream` — the tls-handshake terminates before it. **No nginx WS config
  change beyond the TLS listener.**
- `X-Forwarded-Proto $scheme` already lets the backend see `https` when relevant.
- Hard gating requirement: prod `CORS_ORIGINS` must include the https origin, else
  `origin_allowed()` rejects browser sockets (**NEEDS VERIFICATION** item).
- Browsers will not mix an `Origin` over the plaintext `:80` server — redirect handles it.
- No `ws`-scheme URL literals anywhere in frontend runtime paths after the prod bake
  (verified in M2/M3 hardening): connect-src stays `'self'`.

---

## 10. HSTS Strategy

- **Never send HSTS until real, CA-trusted HTTPS is confirmed working** (also never with
  self-signed/staging certs — see §7). HSTS is a 2-phase rollout:
  - **Phase A (staging):** HSTS disabled (`Strict-Transport-Security` header absent).
  - **Phase B (production-1):** after real cert verified via §15 checks, enable
    `max-age=31536000` on the 443 server (≈1y), `includeSubDomains` omitted (no
    subdomains today — add only if a subdomain will genuinely serve this app),
    `preload` **not** requested (registry submission is an explicit later decision).
    Header added with `always` so error pages carry it.
  - Optional phase C later: short-max-age pilot (`max-age=300`) for a day before the
    full 1y, and/or HSTS preload submission only on operator decision.
- Note the Strict-Transport-Security header is honored by browsers **only when the
  response was delivered over HTTPS**; the 80→443 redirect server should not set it.
- If HTTP is fully blocked (§5 variation), HSTS is moot for the edge but still protects
  browser cache after the first HTTPS visit — keep the header.

---

## 11. Security Headers

Current edge headers are **COMPLETE** and need no change for TLS save two items:

| Header | Today | After TLS |
|---|---|---|
| `Strict-Transport-Security` | **MISSING** | add per §10 (only with real certs) |
| `Content-Security-Policy` | `default-src 'self' … connect-src 'self'; script-src/style-src 'unsafe-inline'` | unchanged (same-origin still holds; optional `upgrade-insecure-requests` may be appended once HTTPS is canonical — purely advisory since all resources are same-origin) |
| `X-Frame-Options` / `X-Content-Type-Options` / `Referrer-Policy` / `Permissions-Policy` | set `always` | unchanged |
| `server_tokens off` | set | unchanged |

---

## 12. Compose Changes Required

All in `docker-compose.prod.yml`; `docker-compose.yml` (dev) stays untouched.

1. **nginx service**
   - `ports`: publish BOTH `${NGINX_HTTPS_PORT:-443}:443` and `${NGINX_HTTP_PORT:-80}:80`.
   - `volumes`: add `./infra/certs:/etc/nginx/certs:ro` (gitignored cert dir).
   - `environment`: pass TLS knobs the nginx config consumes, e.g. `HSTS_ENABLED`,
     `SERVER_NAME` (rendered into the config via envsubst or nginx `map`; or templated
     `nginx.conf` — the config is mounted ro, so templating must happen at image build or
     via envsubst entrypoint — **implementation detail to decide**, flagged for the build
     step).
   - cert paths must not be baked into the base image; they resolve to the ro mount.
2. **api**
   - `ports: !override []` (strip host publish).
   - `environment`: add `CORS_ORIGINS: ${CORS_ORIGINS:-https://<operator domain>}` so WSS
     origin checks pass; keep `TRUSTED_PROXIES`/`APP_ENV` as-is.
3. **web** — `ports: !override []`.
4. **postgres, redis** — `ports: !override []` (never externally exposed in prod).
5. **prometheus, grafana** — `ports: !override []` (internal monitoring only; public
   dashboard exposure is a later M8 posture decision).
6. `.env`: new documented keys — `NGINX_HTTPS_PORT` (default 443), `HSTS_ENABLED`,
   `SERVER_NAME`/`DOMAIN`, `CORS_ORIGINS` (prod https value), cert filenames if not
   canonical. `.env` stays gitignored; add the names to `.env.example` as part of M7
   reconciliation.

Check: after the change, `docker compose … config` must show exactly two published
ports (`443`, `80`) — this is the assertion that replaces the current 7-host-port state.

---

## 13. Files to Modify

| File | Change (future implementation) |
|---|---|
| `infra/nginx/nginx.conf` | add `server { listen 443 ssl; ssl_certificate /etc/nginx/certs/…; ssl_certificate_key …; ssl_protocols TLSv1.2 TLSv1.3; }`; convert existing `listen 80` block into the redirect server (`return 301 https://$host$request_uri;`); add `Strict-Transport-Security` conditionally; keep all locations/upstreams/headers as-is |
| `docker-compose.prod.yml` | nginx ports/volumes/env; `ports: !override []` on api, web, postgres, redis, prometheus, grafana; `CORS_ORIGINS` on api |
| `.env` | new keys (§12.6); `.gitignore` already excludes it |
| `.gitignore` | add `infra/certs/` |
| `docs/runbook.md` | prod HTTPS access + release of the `http://localhost:8000` prod probes in §2–§3; HSTS notes |
| `Makefile` | optional `prod-certs` helper (generate local CA/cert for staging); `prod-down` unchanged |

---

## 14. Files to Create

| File | Purpose |
|---|---|
| `infra/certs/.gitkeep` + `infra/certs/README.md` | **CREATED** (2026-09-11) — document where cert/key material lives; never commit real keys |
| `infra/nginx/http-https.conf` (or envsubst template) | **NOT NEEDED** for staging — a single `nginx.conf` now carries the 443 + 80-redirect servers; HSTS is simply absent in staging. When real certs land (Phase 3/§10) the header is added to the same 443 server. |
| `docs/tls-deployment.md` (or extend runbook) | deferred to the real-cert phase (§8 inputs first) |
| `scripts/prod-local-tls.sh` (or mkcert instructions) | superseded by the `openssl req -x509` one-liner in `infra/certs/README.md` (regenerates the staging cert) |

---

## 15. Verification Plan

Run after implementation (staging first, then prod-real).

1. `docker compose -f docker-compose.yml -f docker-compose.prod.yml config` → exit 0 and
   exactly two published ports (`443`, `80`).
2. Port emission assertion: `docker compose ps` shows no `:5432` `:6379` `:8000`
   `:3000` `:9090` `:3001`.
3. Staging HTTPS: `curl -k` checks (§7) + `openssl s_client -connect :443` handshake +
   SNI/cert SAN validity.
4. `http://host/` → `301` → `https://host/`; mixed-content absent.
5. WSS subscribe smoke test with a real ticket.
6. Edge health: `/healthz`, `/health/ready`, `/system/status` over HTTPS →
   `trading_mode: safe`, all components ok.
7. SAFE MODE gates: L1–L5 regression tests + live `safe_mode: true`.
8. Full suites: backend unit (463, ≥90% cov), backend integration (102), frontend
   (vitest 66, eslint, tsc).
9. **prod-real phase:** repeat 3–8 with CA cert (no `-k`); then enable HSTS and confirm
   header present on 443 and absent on the 80 redirect; confirm no browser
   cert/HSTS-pinning incidents.
10. `docker inspect`/logs: no restart-OOM vs residual errors, nginx reload zero-downtime.

---

## 16. Risks and Rollback

| Risk | Mitigation | Rollback |
|---|---|---|
| HSTS pinning a self-signed/staging cert | HSTS disabled until real CA cert (§10) | clear browser HSTS cache / visit `http://` is refused while pinned — avoid by the gating |
| Mis-issued cert (SAN/domain mismatch) | §15.3 handshake + SAN check before any HSTS | replace cert files + reload; HSTS not yet sent |
| Port-hygiene regression (`ports: []` reappears) | `!override []` token + point-2 assertion in §15 | revert overlay; base ports return (dev parity), no data change |
| 80-redirect only: long URLs/WS over plain HTTP confusing | frontend never uses `http://` (baked empty endpoints) | n/a |
| WSS rejected due to origin allowlist | §8 input 7 (`CORS_ORIGINS` https origin) verified step 5 | fix env, reload api |
| Renewal outage → expired cert | automated renewal + reload (§6 B); calendar reminder if manual | swap valid cert + reload (seconds) |
| DNS/domain not yet owned by operator | staging phase is fully offline-capable | run in staging mode indefinitely |
| `envsubst`/template mishap replaces runtime config | keep `nginx -t` in CI/gate + `docker exec nginx nginx -t` before reload | reload previous image/config (ro mount) |
| Graceful shutdown unaffected | TLS is config-only; no app-code/signal change | — |

---

## 17. Phase 10 TLS Acceptance Checklist

Legend: `[x]` verified in the **staging** phase (2026-09-11); `[ ]` pending the
real-cert / HSTS phases (Phases 2–4). See `docs/phase10-staging-tls.md` §15 for the
detailed staging evidence.

- [x] HTTPS terminates at nginx; only `:443` (and `:80` redirect) reachable externally.
- [x] postgres/redis never host-exposed in prod (`ports: !override []` + config/predicate).
- [x] api/web only reachable through nginx; `NEXT_PUBLIC_*` stay baked same-origin.
- [x] `http://host` → `301 https://host` (or HTTP fully blocked by design).
- [x] WSS subscribe verified end-to-end over a real ticket (https origin in `CORS_ORIGINS`).
- [x] `/health/ready`, `/system/status`, `/healthz` return `safe` over HTTPS.
- [x] SAFE MODE unchanged and asserted (live + test suite).
- [x] `curl -k` staged self-signed verification pass complete (HSTS off).
- [ ] Real CA cert installed and renewing; HSTS `max-age=31536000` enabled only after.
- [x] Security headers intact on 443 (HSTS intentionally absent in staging per §10).
- [x] Backend (463/102) + frontend (66 + lint + tsc) suites green post-change.
- [x] No OOM/restart regressions during staged prod run (`nginx -t` passed; 12/12 healthy).
- [ ] Zero-downtime `nginx -s reload` during real-cert swap (staging did clean stop/start).
- [ ] Operator inputs from §8 collected before real-cert phase.

---

## Implementation Sequence (safest first — for the implementation step, NOT now)

Phase 0 — **Prereqs**: create `infra/certs/` (gitignored) + local CA tooling script;
confirm compose `!override` support on the installed engine with a throwaway file
(compose v5.5.0 supports it).

Phase 1 — **Staging TLS (self-signed, HSTS OFF)** — smallest trusted step:
1. Add 443-ssl server + 80-redirect to nginx config (HSTS guarded off by env/map).
2. Wire nginx cert mount + 443/80 ports in the prod overlay.
3. Apply `ports: !override []` to api/web/postgres/redis/prometheus/grafana; set
   prod `CORS_ORIGINS`.
4. Generate local CA + `localhost` cert → `infra/certs/`.
5. `make prod-up`; assert exactly {443,80}; curl -k https handshake; 301 redirect;
   WSS subscribe; edge health `safe`.
6. Run full test suites. STOP-CONDITION: all green → Phase 2; else rollback (§16).

Phase 2 — **Real production certificate**:
7. Operator provides §8 inputs (domain, DNS A/AAAA to host, CA/email, HTTP-01 vs DNS-01).
8. Issue real cert → same `/etc/nginx/certs` paths; `nginx -t` + reload
   (keeps 80 open for HTTP-01; alternatively DNS-01 then optionally drop 80).

Phase 3 — **HSTS only now**:
9. Enable `Strict-Transport-Security` `max-age=31536000` on 443; confirm header on 443,
   absent on 80; re-run §15 smoke checks. (Preload registration is a future separate
   operator decision.)

Phase 4 — **Docs & sign-off**: runbook/tls-deployment doc, M1 marked DONE in
threat-model §28, acceptance checklist §17 ticked, SAFE MODE re-asserted.