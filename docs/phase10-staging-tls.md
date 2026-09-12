# Phase 10 — Staging TLS

Date: 2026-09-11. Status: **DONE — local/staging HTTPS verified through
`https://localhost`**. Scope: self-signed certificate + Nginx TLS termination +
HTTP→HTTPS redirect + production port hygiene. **This is the staging phase only.**
HSTS and real CA certificates are NOT enabled (see `docs/phase10-tls-audit.md` for
the full plan).

> ⚠️ **The certificate used here is SELF-SIGNED and is NOT suitable as the final
> public production certificate.** It exists solely to exercise the complete
> HTTPS/WSS path (`browser → HTTPS → Nginx → app`) locally. The real production
> phase replaces the same mounted files with CA-issued certificates and only then
> enables `Strict-Transport-Security`.

## 1. Purpose

Deliver a working HTTPS production-overlay so the entire application can be tested
through `https://localhost`:

```
Browser ──HTTPS(443)──> Nginx ──http──> Frontend / API / Workers / internal services
                └─HTTP(80)──> 301 ──> HTTPS
```

- ALL app/API/WSS/health traffic terminates TLS at the edge.
- `postgres`, `redis`, `api`, `web`, `prometheus`, `grafana` are no longer published
  to the host (fixes the audit's `ports: []` limitation), keeping DB/Redis internal.
- Existing security controls preserved: headers, CSP, API routing, WS upgrade,
  `X-Forwarded-Proto`, trusted-proxy client IP / rate limiting, M5 resource limits,
  and every health check.
- SAFE MODE remains enforced end-to-end.

## 2. Certificate Configuration

- Location (per TLS audit): **`infra/certs/`** → mounted read-only into Nginx as
  `/etc/nginx/certs`.
- Files:
  - `infra/certs/fullchain.pem` — self-signed chain, CN=`localhost`,
    SAN=`DNS:localhost, IP:127.0.0.1, IP:::1`, RSA-2048/SHA-256, valid 365 days
    (2026-09-11 → 2027-09-11).
  - `infra/certs/privkey.pem` — private key, mode `600`.
- Nginx references: `ssl_certificate /etc/nginx/certs/fullchain.pem;`
  `ssl_certificate_key /etc/nginx/certs/privkey.pem;`
- Regeneration one-liner (already documented in `infra/certs/README.md`):
  ```sh
  cd infra/certs
  openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes \
    -keyout privkey.pem -out fullchain.pem \
    -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1"
  chmod 600 privkey.pem
  ```

## 3. Nginx HTTPS Configuration

`infra/nginx/nginx.conf` now contains a `listen 443 ssl` server (`http2 on`,
TLSv1.2 + TLSv1.3, session cache) carrying the **unchanged** request pipeline:

- `server_name _` (default vhost; single staging cert).
- Security headers (all `always`): `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`,
  `Permissions-Policy`, and the project-derived CSP (`default-src 'self'`,
  `script-src 'self' 'unsafe-inline'` (Next RSC inline), no `'unsafe-eval'`,
  `connect-src 'self'`, `frame-ancestors 'none'` …) — identical to the pre-TLS build.
- `server_tokens off` keeps banner hidden.
- All locations preserved: `/api/` (REST + WS upgrades), `/ws/`,
  `/health/live`, `/health/ready`, `/system/status`, `/healthz`, `/` (Next.js web).
- `proxy_set_header X-Forwarded-Proto $scheme;` → the API receives `https` (and it
  lands in the trusted-proxy model from M3, so client-IP/rate-limit behaviour is
  unchanged; `X-Real-IP`/`X-Forwarded-For` logic untouched).
- **No `Strict-Transport-Security` header** (staging rule from the audit).

## 4. HTTP Redirect

Second `server` block: `listen 80` → `return 301 https://$host$request_uri;`

- Serves only the redirect; no content, no cookies, no security-header additions.
- Verified: `curl -sI http://localhost/` → `HTTP/1.1 301 Moved Permanently`,
  `Location: https://localhost/`.
- The frontend never uses `http://` (prod build bakes same-origin endpoints), so no
  mixed-content or WS-on-HTTP paths exist.

## 5. Production Port Exposure

Applied the audit's identified mechanism `!override []` in `docker-compose.prod.yml`
(Compose spec tag; supported by Compose v5.5.0). This genuinely clears the base
(dev) `ports` lists that plain `ports: []` left intact.

| Service | Before (audit) | After staging TLS |
|---|---|---|
| `nginx` | `:80` | **`:443` + `:80`** only |
| `api` | `:8000` | internal only (`!override []`) |
| `web` | `:3000` | internal only (`!override []`) |
| `postgres` | `:5432` | internal only (`!override []`) |
| `redis` | `:6379` | internal only (`!override []`) |
| `prometheus` | `:9090` | internal only (`!override []`) |
| `grafana` | `:3001` | internal only (`!override []`) |

Verified three ways:
1. `docker compose config` (JSON) → published ports: **only `nginx` = `['443','80']`**,
   every other service `(none)`.
2. `docker compose ps` → only nginx shows `0.0.0.0:80->80/tcp` and
   `0.0.0.0:443->443/tcp`; all other services show internal-only (`8000/tcp` etc. with
   no `->` mapping).
3. Host `ss -ltn` → **only `:80` and `:443` listening** (no 5432/6379/8000/3000/9090/3001).

Internal Docker networking (compose default network, pinned `172.28.0.0/16`) is
unchanged; Nginx↔API↔web↔DB↔Redis↔monitoring all still communicate normally.

Additional compose changes in the staging phase:
- `nginx` mounts `./infra/certs:/etc/nginx/certs:ro` and publishes
  `${NGINX_HTTPS_PORT:-443}:443` + `${NGINX_HTTP_PORT:-80}:80`.
- `api` environment adds `CORS_ORIGINS: ${CORS_ORIGINS:-https://localhost}` so the
  browser `Origin: https://localhost` passes the WSS allowlist (`ws/hub.py`) under
  HTTPS — same-origin CORS is unaffected.
- **M5 resource limits are fully preserved** (identical `deploy:` anchors remain on
  all 12 services).

## 6. API Verification

Through `https://localhost` (Python `httpx`, CA=the self-signed `fullchain.pem`):

| Check | Result |
|---|---|
| `POST /api/v1/auth/register` | 201 |
| `POST /api/v1/auth/login` | 200 (refresh cookie path active) |
| `POST /api/v1/ws/ticket` (Bearer) | 200 |
| `GET /api/v1/auth/me` (Bearer) | 200, correct mailbox — authenticated RBAC read over HTTPS |

The full API surface is reachable only behind Nginx over HTTPS now (no `:8000`
host port exists).

## 7. Frontend Verification

- `GET https://localhost/` over HTTPS → `HTTP/2 200`, all security headers + CSP
  present on the document.
- HTML references only same-origin `/_next/static/...` assets; **0 plain-`http://`
  references** in the served document (prod build bakes same-origin API/WS).
- Frontend therefore talks to the API exclusively through the same-origin HTTPS path
  (`https://localhost/api/v1/...`, `wss://localhost/api/v1/ws/...`) — matching
  `connect-src 'self'`.

## 8. WebSocket Verification

- `wss://localhost/api/v1/ws/stream?ticket=…` (real one-time ticket, CA-verified SSL
  context) → `subscribe` on `alerts` → **`subscribed` ack** received within 5s.
- Performed via Python `websockets` (the browser-equivalent same-origin WSS path);
  Nginx performs the TLS handshake then the existing `Upgrade` proxying to
  `http://api_upstream`. No WS config change was required beyond the TLS listener.

## 9. Health/Readiness Verification

| Endpoint (HTTPS) | Result |
|---|---|
| `/healthz` (→ `/health/live`) | 200 `{"status":"ok","mode":"safe"}` |
| `/health/ready` | 200 `{"status":"ok","mode":"safe"}` |
| `/system/status` | 200, `app_env=prod`, database/redis/migrations `ok` |

Container healthchecks (`http://127.0.0.1:8000/...` inside the api container) are
TLS-independent and all reported `(healthy)`.

## 10. SAFE MODE Verification

- `GET /system/status` over HTTPS: **`trading_mode=safe`** and **`safe_mode=true`**.
- `/health/live` + `/health/ready` both report `mode=safe`.
- SAFE MODE L1–L5 regression coverage still green in the automated suites (§12).

## 11. Certificate/Secret Git Hygiene

- `.gitignore` adds `infra/certs/*` with allow-list exceptions for
  `!infra/certs/.gitkeep` and `!infra/certs/README.md`.
- `git check-ignore -v` confirms **both `fullchain.pem` and `privkey.pem` are
  ignored**; `git status --untracked-files=all infra/certs` shows only
  `.gitkeep` + `README.md` as trackable.
- No `.pem`/`.key`/`.crt` files are staged or untracked anywhere else.

## 12. Test Results

| Suite | Result |
|---|---|
| Backend unit (`pytest tests/unit --cov=app`) | **463 passed**, coverage **92.61%** (gate ≥90%) |
| Backend integration (`pytest tests/integration`) | **102 passed** |
| Frontend `npm test` (vitest) | **66/66 passed** (4 files) |
| Frontend `npm run lint` | exit 0 |
| Frontend `npx tsc --noEmit` | exit 0 |
| `docker compose … config -q` | exit 0 |
| `nginx -t` (inside container) | syntax ok / test successful |
| Nginx log TLS/error scan | 0 error lines |

## 13. Rollback Procedure

To revert to plain-HTTP (pre-TLS) behavior:

```sh
# 1. stop the overlay
docker compose -f docker-compose.yml -f docker-compose.prod.yml down --remove-orphans
# 2. restore pre-TLS files (git): infra/nginx/nginx.conf, docker-compose.prod.yml
#    (drop 443 port, cert mount, CORS_ORIGINS override, and the !override [] port
#    hygiene — or keep hygiene, it is independent of TLS)
# 3. bring the stack back
docker compose up -d          # dev, or the prod overlay again
```

Rollback is non-destructive: volumes persist, no schema/application changes occurred,
and the self-signed material simply becomes unused. `infra/certs/` may be deleted
entirely if reverting — images/volumes do not reference it after the mount is removed.

## 14. Limitations

- **Self-signed certificate** — browsers will present a certificate warning when the
  operator opens `https://localhost`. This is expected and correct for staging; it is
  NOT the final production certificate.
- **HSTS intentionally absent** — enabling HSTS against a self-signed cert would hard
  pin the browser cache to this cert.
- `NGINX_HTTPS_PORT`/`NGINX_HTTP_PORT` default to 443/80 and are overridable via
  `.env`; there is no port remap evidence yet (defaults used throughout).
- The `CORS_ORIGINS` default in the overlay is now `https://localhost`; a production
  deployment must set it to the real `https://<domain>` before the CA phase.
- Real production needs (Let's Encrypt/DNS-01/HTTP-01, domain, A/AAAA record, renewal,
  HSTS) are deliberately NOT configured — see §8 of `docs/phase10-tls-audit.md`.

## 15. Acceptance Checklist

- [x] Local/staging self-signed cert generated for `localhost` (SAN incl. 127.0.0.1/::1).
- [x] Cert/key stored under `infra/certs/`; key mode 600; **not committable (gitignored)**.
- [x] Nginx listens on 443 (TLS1.2/1.3) using the staging cert, preserves all headers,
      CSP, API routing, WS upgrade, `X-Forwarded-Proto`, and trusted-proxy client IP.
- [x] HTTP 80 → 301 → HTTPS.
- [x] Only ports **80** and **443** externally published (`config`, `ps`, `ss` verified);
      postgres/redis/api/web/prometheus/grafana internal-only (`!override []`).
- [x] M5 resource limits untouched; all health checks preserved.
- [x] Full suite: `docker compose ps` all healthy (12/12).
- [x] TLS handshake: `curl -k -I https://localhost` → 200; `openssl s_client` shows
      CN=localhost self-signed as expected; HTTP 301 verified.
- [x] API over HTTPS: register/login/ticket/auth-me all 200/201.
- [x] Frontend over HTTPS: same-origin assets, 0 plain-http refs.
- [x] WSS: real ticket → `subscribed` ack.
- [x] `/health/ready` + `/system/status` → `safe`; `trading_mode=safe`,
      `safe_mode=true`.
- [x] Automated suites green (463 unit / 102 integration / 66 vitest / lint / tsc).
- [x] Nginx logs free of TLS/error lines; stack stops cleanly; dev stack restored.
- [x] No secrets, private keys, or certs committed.