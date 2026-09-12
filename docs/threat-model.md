# Threat Model — Forex AI System (SAFE MODE)

Documented against the **actual current implementation** (branch `main`, 19 commits, audited
2026-09-10). SCOPE: Phase 10 documentation deliverable. This file is evidence-based — it cites
real modules; nothing below claims a control that does not exist. Unverifiable items are marked
**NEEDS VERIFICATION**.

SAFE MODE context: the system performs **paper trading only**. There is no live order execution
and no broker adapter (`TRADING_MODE` rejects anything but `safe`). Threats here target a paper-only
analysis system; live-money threats are out of scope and structurally impossible by design.

---

## 1. System overview

| Component | Implementation (evidence) |
|---|---|
| API | FastAPI, Python 3.12, many routers under `api/v1/`, problem+json errors (`app/core/errors.py`) |
| Workers | 5 roles: `ingest`, `agents`, `content`, `orchestrator`, `alerts` (`app/worker_main.py`, compose services) |
| Agents | technical/regime/fundamental/sentiment, deterministic fallbacks, optional LLM via `LLMClient` (`app/llm/client.py`, ADR-0004) |
| Orchestrator + risk | single-owner lock; fuses signals; risk gates produce ANALYSIS/PAPER intents only (`app/decisions/*`, `app/workers/orchestrator_runtime.py`) |
| Backtester | deterministic `PaperBroker` over history (`app/backtest/*`, `app/broker/paper.py`) |
| Data stores | PostgreSQL 16 (migrations head `0007`), Redis 7 (streams/heartbeats/locks), both compose-managed |
| Event bus | Redis Streams: `bars.closed.{tf}`, `signals.stream`, `decisions.stream`, `alerts.stream`; pub/sub `prices.live` (`app/bus/topics.py`) |
| Realtime | WebSocket observer feed over streams, one-time Redis tickets (`app/ws/tickets.py`, `app/ws/hub.py`) |
| Frontend | Next.js 15 (App Router), session tokens in `sessionStorage`, refresh in HttpOnly cookie (`frontend/src/lib/auth.ts`) |
| Reverse proxy | nginx (prod overlay only): API strip, WS upgrade, security headers, `server_tokens off` (`infra/nginx/nginx.conf`) |
| Monitoring | Prometheus (api `/metrics` + 5 worker exporters 9101–9105), Grafana provisioned dashboard, truthful live probes |
| Packaging | Docker compose dev/prod; multi-stage, non-root prod images; CI: ruff/mypy/pytest/eslint/tsc/build (see `.github/workflows/`) |

---

## 2. Assets that require protection

| Asset | Sensitivity | Storage/owner |
|---|---|---|
| User accounts & credentials | HIGH | Postgres `users` (Argon2id hashes), `refresh_tokens`, `audit_log` |
| API/refresh tokens, WS tickets | HIGH | JWT in client memory; refresh cookie HttpOnly; tickets in Redis w/ 30s TTL |
| `SECRET_KEY` (JWT signing) + DB/Redis passwords | HIGH | Environment only; `.env` gitignored |
| Candle data, signals, decisions, backtest runs | MEDIUM | Postgres (`candles`, `signals`, `decisions`, `backtests`), Redis streams |
| Operator emails / role metadata | MEDIUM | Postgres `users` |
| LLM API keys (opencode_zen), market-data keys (oanda/finnhub) | HIGH | Environment only (empty in default dev) |
| Metrics / dashboards | LOW | Prometheus, Grafana (public in dev) |
| Worker identity/heartbeat state | LOW–MED | Redis |
| Config/containers | MEDIUM | Compose files, Dockerfiles |

---

## 3. Trust boundaries

| Boundary | Participants | Notes |
|---|---|---|
| TB1 Browser ⇄ nginx/api | nginx (prod) or direct FastAPI (dev) | TLS only in prod overlay (nginx `listen 80` today) |
| TB2 API ⇄ Postgres | FastAPI ⇄ postgres | Dev exposes 5432 on host; prod overlay removes host port |
| TB3 API/workers ⇄ Redis | app ⇄ redis | No auth on Redis; dev exposes 6379 on host |
| TB4 Workers ⇄ API ⇄ streams | internal | All inside compose network |
| TB5 Prometheus ⇄ api/workers exporters | internal (dev also host-visible) | `/metrics` unauthenticated (documented) |
| TB6 Frontend ⇄ external feeds | none in default (synthetic) | oanda/finnhub optional, keyed, practice-only |
| TB7 LLM provider ⇄ agents | optional, disabled by default (`llm_provider=none`) | outbound HTTPS only when keyed |

---

## 4. Entry points / interfaces

| Entry point | Auth | Control notes |
|---|---|---|
| `/api/v1/auth/*` (register/login/refresh/logout/me) | public (self-service) | rate-limited per IP; first user becomes admin |
| `/api/v1/*` routers (users/admin/signals/content/decisions/risk/market/backtests/alerts) | Bearer access token, RBAC viewer<trader<admin | `api/deps.py` |
| `/health/live`, `/health/ready`, `/system/status`, `/metrics` | public | readiness gate + safe mode surfaced (L4) |
| `/ws/…` realtime | one-time 30s Redis ticket (`GETDEL`) + Origin check | observer-only reads; topic RBAC |
| Nginx (prod) | – | strips `/api/` to `api:8000`, `:80` only (no TLS yet) |
| Docker host exposed ports (dev) | – | 8000, 3000, 5432, 6379, 9090, 3001 |

---

## 5. Authentication and authorization

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Password guessing/credential stuffing | Argon2id; per-IP sliding-window limits login=10/min, register=5/min, refresh=30/min (`core/ratelimit.py`, `api/deps.py`) | Limiter is in-process (resets on restart); per-IP based on direct peer only — behind nginx all users share one IP | Optional Redis-backed limiter; honor `X-Forwarded-For` behind trusted proxy; document baselines | YES (proxy IP handling; Redis limiter optional later) |
| Weak password policy | None (only min length from Pydantic schema) | No strength/complexity/breach checks | Document acceptable password policy; schema-level additions later (out of Phase 10 scope unless advisory) | NO (document only) |
| JWT forgery | HS256 signed with `SECRET_KEY`; prod rejects the insecure default (`config.py:178-185`) | Key delivers via env; weak key in dev | Prod secret generation + rotation procedure (runbook) | YES (rotation runbook) |
| Token theft (access) | 30-min access tokens; refresh delivered HttpOnly SameSite=Lax cookie, Secure in prod | Access token stored in `sessionStorage` (XSS-readable) | CSP to blunt XSS; short TTL already; document | YES (CSP) |
| Refresh-token reuse/theft | Rotation per refresh; reuse revokes the whole family (`auth_service.py:227-264`, `test_token_rotation.py`) | Reuse detection is per-family in Postgres — sound | None | NO |
| Cookie flags via API | HttpOnly, SameSite=Lax, Secure when prod | In prod overlay the app sees `http://` internally → `cookie_secure` only True because `app_env==prod`; client channel is HTTP:80 until TLS lands | Enable TLS so Secure cookies are usable end-to-end | YES (with TLS item) |
| First-user admin bootstrap | First registered user becomes admin (`auth_service.py:110`) | If an attacker registers first, they own admin | Document; protect registration rate-limit; consider invite/flag | NO (document) |
| Disabled/deleted accounts | `is_active` checked on login/refresh/current-user | – | – | NO |

---

## 6. API security

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Unauthorized access | RBAC enforced per router (`require_role`); 401 on missing/invalid token | – | – | NO |
| Error info leakage | Uniform problem+json; 500 handler returns generic message w/ correlation id (`errors.py:205-221`) | `_check_db`/detail strings may echo low-level text; validation errors expose field names | Keep; spot-check no sensitive fields in `detail` | NO |
| Cross-site requests (CSRF) | State-changing auth uses Bearer header (not cookies); refresh via SameSite=Lax cookie; CORS allowlist | GET endpoints are read-only | – | NO |
| CORS abuse | `cors_origins` allowlist (default localhost:3000) | – | – | NO |
| Bounced headers / spoofing | `client_ip` honours `X-Real-IP`/`X-Forwarded-For` ONLY from CIDRs in `TRUSTED_PROXIES` (`app/core/config.py` → `app/api/deps.py`); nginx overwrites inbound headers and sets `X-Real-IP=$remote_addr`; prod overlay pins the network subnet and sets `TRUSTED_PROXIES=172.28.0.0/16` | Direct/documented dev mode (trusted_proxies empty) keeps peer-only behaviour; Redis-backed limiter still deferred | None (implemented). Optional later: Redis-backed global limiter | NO (implemented) |
| Brute force on non-auth endpoints | None | e.g. `/market/candles`, signals | Consider global limiter (optional) | NO (document) |
| Request-size/body abuse | None explicit (nginx `client_max_body_size 1m` in prod; Starlette defaults) | Upload-style abuse minimal (JSON only) | Accept; no file uploads exist | NO |
| Unauthenticated `/metrics` | Public by design in dev (`infra/prometheus/prometheus.yml`) | Exposes internals if reached | Network-isolate or token-auth in prod | YES (document decision) |

---

## 7. Frontend security

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| XSS → token exfiltration | Access token in `sessionStorage`; no DOM sinks; authFetch attaches Bearer; CSP at nginx edge: `default-src 'self'`, `script-src 'self' 'unsafe-inline'`, no `'unsafe-eval'`, `connect-src 'self'`, `frame-ancestors 'none'` | CSP is edge-only (not yet set by backend middleware for direct dev/prod API access) | Add CSP to backend middleware (optional follow-up) | NO (implemented at edge) |
| Token leakage in URLs/history | Tokens never placed in URLs; WS uses one-time ticket not JWT (`ws/tickets.py`) | – | – | NO |
| Open redirect / unsafe links | No user-supplied redirects | – | – | NO |
| Supply-chain (node_modules) | `npm ci` w/ lockfile; Dependabot npm weekly | No `npm audit` gate in CI | Add `npm audit` step | YES |
| Secrets in client bundle | Only `NEXT_PUBLIC_API_URL`/`WS_URL` exposed | Non-public env must not start with `NEXT_PUBLIC_` | Document; CI guard optional | NO (document) |

---

## 8. WebSocket security

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Unauthenticated socket | Mandatory one-time ticket redeemed atomically via `GETDEL` (30s TTL) — no JWT in URL (`ws/tickets.py:86-98`) | – | – | NO |
| Cross-Site WebSocket Hijacking | `origin_allowed()` checks exact `Origin` against `cors_origins` (`ws/hub.py:102-115`) | Missing `Origin` (non-browser) is permitted — acceptable, still ticket-authenticated | Document | NO |
| Topic privilege escalation | Per-topic min role; unknown/reserved topics rejected (`ws/tickets.py:43-49`) | – | – | NO |
| Poison/malformed events | Cursor advances past poison entries; bounded control frames (4096B) (`ws/hub.py`) | – | – | NO |
| Resource exhaustion (many sockets) | None (no per-IP/per-user connection cap) | Memory/CPU exhaustion via connection flood | Optional cap/backpressure; document | NO (document) |
| Data disclosure on topics | Initial data from REST; sockets see only post-subscribe events | – | – | NO |

---

## 9. Worker/service communication

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Split-brain (two orchestrators) | Single-owner Redis `lock:orchestrator` token; lock re-acquisition respects lease (`runbook.md §4.1`) | Lock is redis-based (single point); no fencing token on DB writes | Document lock semantics; no change unless HA | NO |
| Two ingest workers | `lock:ingest:{provider}` (`bus/topics.py:31-33`) | – | – | NO |
| Work duplication (at-least-once) | Alerts group `alerts` on `alerts.stream`; idempotent persistence by content hash/keys | Possible duplicate alert rows (dedup by unique event_id `ix_alert_events_event_id`) | – | NO |
| Poison-message loops | Streams tolerate poison; cursors advance | – | – | NO |
| Worker compromise → DB/Redis abuse | Internal network only; Postgres/Redis unreachable from host in prod overlay | – | – | NO |

---

## 10. PostgreSQL security

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Remote access | Dev publishes 5432 on host; prod overlay removes host port | Dev exposure to LAN; default dev password possible if `.env` not customized | Document; use non-default creds; keep prod host-less | YES (document + prod posture) |
| SQL injection | SQLAlchemy ORM/parameterized queries; `session.execute(text(...))` only for fixed strings (`system.py`) | – | – | NO |
| Least privilege | Single app role owns schema | Role has full CRUD; no separate read-only cop | Optional role split later | NO |
| Data at rest | None (no encryption/encrypted volumes) | DB dump = plaintext | Guide encrypted-volume/at-rest option; backups gzip | YES (backup doc) |
| Backups missing | None | No `pg_dump` script; data loss on volume failure | `scripts/backup.sh` + `make backup/restore` + documented restore drill | YES |

---

## 11. Redis security

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Unauthenticated access | None (no `requirepass`) — relies on network isolation; dev publishes 6379 on host | LAN/compromise exposure in dev; any container breach reads all Redis (tickets, locks, prices) | Document; optional `requirepass` + ACL; keep host port dev-only | YES (document/prod posture) |
| Data tampering (streams/heartbeats) | Heartbeats TTL-scrubbed; gauges recompute; consumers tolerate malformed | – | – | NO |
| Key/data leakage via metrics | Staleness/prices probes only export aggregates | – | – | NO |

---

## 12. Market-data ingestion risks

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Untrusted/external provider data | Provider abstraction (ADR-0003); synthetic default; oanda limited to `practice` env (`config.py:245-254`); breaker after 5 failures / 60s cooldown | Real feed vetted? unverified as not used in default env | Document provider trust + risk approval if real feed enabled | NO (document) |
| API-key leakage for oanda/finnhub | Keys env-only; redaction in logs | – | – | NO |
| Data poisoning (bad candles) | Synthetic deterministic; no market_open math on candles themselves; staleness watchdog | If a provider feeds anomalies, agents/orchestrator accept them | Optional validation/filtering later; document | NO (document) |
| Malformed payloads | Providers parse+normalize; persistence idempotent/dedup | – | – | NO |

---

## 13. AI-agent risks

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Agent output garbage → analysis/decisions | Agents normalize to schemas/typed signals; fusion threshold; only analysis/paper intents; min coverage 0.5 gates orchestration (`config.py:125-137`) | Model errors degrade analysis quality, not safety | Keep deterministic fallbacks; document | NO |
| LLM provider compromise (optional) | `llm_provider=none` default; `LLMClient` only interface; daily budget breaker (`llm/client.py`); max tokens/timeouts | If enabled, prompts could be misrouted; cost runaway bounded | Document enablement + budget OSV/authz maturity | NO (document) |
| Agent code injection | Agents execute server-controlled code only; no arbitrary user-supplied tool execution | – | – | NO |

---

## 14. Prompt/input/data risks

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Prompt injection via ingested content (news/calendar) | No user-facing LLM chat; agents synthesize from normalized DB records; LLM disabled by default | When LLM enabled, crafted news could steer text output — rest of pipeline is deterministic/numeric | Document; keep LLM advisory-only | NO (document) |
| Oversized/normalization abuse | Schema validation (Pydantic) on all inputs | – | – | NO |

---

## 15. Trade-decision / orchestrator risks

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Unsafe order/execution path | No executor role exists (`worker_main.py:62-64` refuses); only PAPER ANALYSIS/BLOCKED intents; decisions carry no order routing (`models/decision.py`) | – | – | NO |
| Decision flapping | Hysteresis band, per-pair cooldown (`config.py:133-137`) | – | – | NO |
| Garbage-in → risk | Risk agent is final gate; min coverage fails closed; risk can block entries (`decisions/risk.py`) | – | – | NO |
| Coordinated agent corruption | Fusion + agreement thresholds; independent agents | – | – | NO |

---

## 16. Risk-management controls

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Over-exposure | Risk knobs: max risk/exposure/daily loss/drawdown/correlation caps; min R:R; ATR-based SL/TP (`config.py:139-161`) | Paper-only math, no live enforcement needed | – | NO |

---

## 17. SAFE MODE controls

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Accidental live execution | L1 config rejects non-`safe`; L2 only `PaperBroker`; L3 orchestrator/risk gates; L4 startup+health assertions; L5 `safety`-marked CI tests (`docs/safe-mode.md`, live probes) | None identified | Keep regression suite run in every CI (already does) | NO (verify still green at sign-off) |
| Regression disabling guards | `test_config_safe_mode.py`, `test_decision_safety.py` (12 tests), `test_security.py`, `test_app_factory.py`, integration safety tests | – | – | NO |

---

## 18. Secrets / configuration risks

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Committed secrets | `.env` gitignored; only `.env.example` committed | `.env` drift vs `Settings` keys (legacy names like `NEWS_API_KEY`) | Reconcile `.env.example` with actual settings | YES (doc/cleanup) |
| Default Grafana creds | Dev default admin/admin + anonymous Viewer | Reachable on LAN (dev); not in prod overlay | Rotate/suppress anonymous in prod; document | YES (document) |
| Insecure dev `SECRET_KEY` in prod | Config fails closed (`config.py:178-185`) | prod-up halts until real key set — by design | Document in runbook; close loop with TLS item | YES (document) |
| Key rotation | None | Long-lived secrets never rotated | Add secrets-rotation runbook section | YES |

---

## 19. Logging and sensitive-data exposure

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Passwords/tokens in logs | structlog redaction of keys matching password/token/secret/api_key/cookie/authorization/credential (`core/logging.py:26-69`) | Redaction is key-name based; a value under an unredacted key could leak | Keep; spot-check sensitive payloads; test redaction | NO (verify at sign-off) |
| Correlation ID for forensics | `asgi-correlation-id` middleware; IDs on responses and errors | – | – | NO |

---

## 20. Docker / container risks

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Privileged user in containers | Non-root `appuser`/`node` in prod images (`backend/Dockerfile:25-27`, `frontend/Dockerfile:32`) | Dev images run as root (bind-mount convenience) | Dev-only; document | NO (document) |
| OOM thrash | None | No resource limits → noisy-neighbor/OOM | Add `deploy.resources.limits` in prod overlay | YES |
| Image supply chain | Multi-stage; bases pinned to version tags | Not digest-pinned; no image CVE scan | Optional digest pinning; **trivy in CI** | YES (trivy) |
| Exposed host ports (dev) | Compose defines explicit dev ports | 5432/6379/9090/3001/3000/8000 all on host | Dev-only; prod overlay suppresses | NO (document) |

---

## 21. Nginx / reverse-proxy risks

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Eavesdropping (plaintext) | None (no TLS; `listen 80`) | Credentials/session cookies traversable in cleartext over network in prod-like deployments | nginx TLS (443, HSTS, 80→443 redirect), cert management | YES |
| Header injection / proxy confusion | `client_ip` honours forwarded headers only from `TRUSTED_PROXIES` CIDRs; nginx sets `X-Real-IP`/`X-Forwarded-For`/`X-Forwarded-Proto` and strips inbound ones; verified live via spoofed-header test through nginx | – | – | NO (implemented) |
| Header stripping (nosniff/Frame-Options) | nginx sets X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, CSP; `server_tokens off` | Headers absent on direct api/web (dev) | CSP + security headers at FastAPI/Next level too | NO (implemented at edge) |
| Slowloris/body abuse | `client_max_body_size 1m`; WS read timeout 3600s | – | – | NO |

---

## 22. Dependency / supply-chain risks

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Known-vulnerability deps | Version-bounded ranges (`pyproject.toml`, `package.json`+lockfile); Dependabot weekly/monthly | Vulnerable release may go unnoticed until Dependabot PR | Add `pip-audit` (backend CI), `npm audit` (frontend CI), trivy (docker-build CI) | YES |
| Malicious package tampering | npm ci from lockfile | Private registry not pinned | Keep lockfiles; optional registry pinning | NO (document) |

---

## 23. Monitoring / alerting risks

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Blind spot (worker down) | Truthful healthchecks; heartbeats+TTL; `/system/status`; worker exporters `forex_worker_up`; staleness alerts (`monitor/staleness.py`) | No paging/out-of-band alerting destination | Alerting route (later); document current scope | NO (document) |
| Prometheus/Grafana exposure | Grafana anonymous Viewer in dev; not in prod overlay | – | Protect in prod | NO |
| Alert fabric noise | Staleness guard uses market-hours + TTL-aware thresholds | – | – | NO |

---

## 24. Backup / recovery risks

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Data loss | Compose named volumes; runbook has restart/rollback guidance | **No DB backup/restore procedure exists** | `scripts/backup.sh`, `make backup/restore`, restore drill, `docs/backup-restore.md` | YES |
| Corrupt volume | – | Volume corruption = full loss | Encrypted backups + retention | YES (backup doc) |

---

## 25. Availability / DoS risks

| Threat | Existing control | Remaining risk | Recommended mitigation | Required for Phase 10 now? |
|---|---|---|---|---|
| Auth flood | Per-IP sliding window on auth endpoints; behind nginx the real client IP is recovered via trusted-proxy headers (verified live: spoofed headers cannot bypass, audit log records `172.28.0.1` from nginx's `X-Real-IP`) | In-process limiter resets on restart; single-api-container scaling invalidates per-process limits | Redis-backed limiter (later); document steady-state capacity | NO (document) |
| WS connection flood | Bounded control frames; ticket per connection | No connection cap/backpressure | Optional caps; document | NO (document) |
| Container crash | `restart: unless-stopped`; healthy-condition deps | Resource caps guard runaway containers | – | NO (implemented) |
| DB/Redis single instance | – | Single points of failure | Out of scope for v1; document | NO (document) |

---

## 26. Threat scenarios

| # | Scenario | Existing control | Remaining risk | Required for Phase 10 now? |
|---|---|---|---|---|
| T1 | Attacker brute-forces login | Argon2id + per-IP 10/min login limit | Redis-backed limiter deferred; single-app scale (documented) | NO (implemented) |
| T2 | Stolen/lost laptop with session | Access TTL 30m; refresh rotation + reuse-detection revokes family | Access token usable to read data until expiry | NO |
| T3 | XSS in dashboard steals token | CSP at nginx edge blocks exfiltration (no `unsafe-eval`, `connect-src 'self'`, `frame-ancestors 'none'`); tokens in sessionStorage | CSP edge-only, not on direct dev API | NO (implemented) |
| T4 | CSRWSH against WS feed | Origin allowlist + one-time ticket | – | NO |
| T5 | Redis exposed on LAN (dev) | Network isolation assumption | Direct access to tickets/locks/prices | YES (document/prod) |
| T6 | DB exposed on LAN (dev) + default creds | `.env`-gated creds; prod strips host port | Credential theft in dev | YES (document) |
| T7 | Traffic sniffed without TLS | nginx :80 only | Credentials/cookies read | YES (TLS) |
| T8 | Deps with known CVEs | Dependabot | Window before PR merges | YES (audit CI) |
| T9 | Container OOM | Per-service memory limits + reservations (prod overlay) | Oversubscribed host still OOMs the kernel | NO (implemented) |
| T10 | Orchestrator split-brain | Redis owner lock | Lock TTL/lease misconfig | NO (document) |
| T11 | Log leakage of tokens | Key-name redaction | Value-level leakage under safe keys | NO (verify) |
| T12 | Full data loss | Volumes only | No usable backup | YES (backup) |
| T13 | Live-trading regression | 5-layer SAFE MODE + safety CI tests | – | NO (verify) |
| T14 | Excessive LLM spend (if enabled) | `LLM_DAILY_BUDGET_USD` breaker; disabled by default | Budget estimate is a placeholder constant (`llm/client.py:52`) | NO (document) |

---

## 27. Existing mitigations (summary of controls that are COMPLETE)

- SAFE MODE L1–L5 with `safety`-marked regression tests in CI.
- Argon2id password hashing; JWT HS256; short access TTL; refresh rotation with family reuse-detection.
- Refresh cookie HttpOnly/SameSite/Lax (+ Secure in prod).
- RBAC viewer<trader<admin enforced on every protected router.
- Per-IP sliding-window rate limits on auth endpoints.
- WS: one-time Redis tickets (`GETDEL`), Origin allowlist, per-topic RBAC, poison-tolerant cursors, bounded control frames.
- Structured logging with secret key-name redaction + correlation IDs; problem+json error contract.
- CORS allowlist; `.env` gitignored; prod rejects insecure `SECRET_KEY`.
- Truthful healthchecks (2026 state), heartbeats + staleness watchdogs, per-worker Prometheus exporters, provisioned Grafana dashboard.
- Non-root prod containers; multi-stage images; version-bounded deps + lockfiles; Dependabot.
- CSP + edge security headers (X-Frame-Options DENY, X-Content-Type-Options nosniff, Referrer-Policy, Permissions-Policy, `server_tokens off`) at nginx.
- Trusted-proxy client-IP recovery: forwarded headers accepted only from `TRUSTED_PROXIES` CIDRs; nginx overwrites inbound headers; verified spoof-resistance through nginx.

---

## 28. Remaining mitigations required by Phase 10

| # | Mitigation | Phase 10 scope | Notes |
|---|---|---|---|
| M1 | nginx TLS (443, HSTS, 80→443) + cert management | Required | sec §21, §3 TB1 |
| M2 | Content-Security-Policy + security headers (API + Next + nginx) | **DONE (nginx edge); backend middleware optional follow-up** | sec §7, §21; verified in `docs/phase10-security-hardening.md` |
| M3 | Trusted-proxy `X-Forwarded-For` handling so rate limits see real clients | **DONE** | sec §5, §6; `TRUSTED_PROXIES` + nginx overwrite + prod subnet pin |
| M4 | Dependency/image CVE audits: pip-audit, npm audit, trivy in CI | Required | sec §22 |
| M5 | Prod resource limits in compose overlay | **DONE** | sec §20; `deploy.resources` in `docker-compose.prod.yml`, verified in `docs/phase10-resource-limits.md` |
| M6 | Backup/restore script + procedure + restore drill | Required | sec §24 |
| M7 | Secrets rotation runbook + Grafana cred policy + `.env.example` reconciliation | Required | sec §18, §2 |
| M8 | Monitoring/alerting posture doc (prometheus/grafana prod access) | Required (document) | sec §23 |
| M9 | Threat-model acceptance & Phase 10 sign-off in runbook | Required | this file §“Acceptance” |

Items explicitly **NOT** required now (documented only): live-broker adapters (forbidden), Redis/Postgres host exposure only in dev, LLM enablement & cost policy, WS connection caps, Redis-backed rate limiter, password-policy enforcement, HA/failover for DB/Redis.

---

## Phase 10 Threat Model Acceptance

Sign-off basis: this model was produced from the audited implementation (`PHASE_10_AUDIT.md`, 2026-09-10). Controls marked “existing” were verified by file inspection and, where noted, live probes.

| Requirement | Documented | Existing control verified | Remaining action | Phase 10 status |
|---|---|---|---|---|
| System overview, assets, trust boundaries, entry points | ✅ §1–§4 | ✅ file-inspected | none | READY |
| Authentication & authorization | ✅ §5 | ✅ `security.py`, `auth_service.py`, `api/deps.py`, tests | Proxy-correct client IP DONE; rotation runbook | PARTIAL |
| API security | ✅ §6 | ✅ routers, errors, CORS; trusted-proxy client IP | Security headers on API (backend middleware, optional) | PARTIAL |
| Frontend security | ✅ §7 | ✅ `auth.ts` (sessionStorage, Bearer); CSP at edge | npm-audit gate in CI | PARTIAL |
| WebSocket security | ✅ §8 | ✅ `ws/tickets.py`, `ws/hub.py` | (none required) | COMPLETE |
| Worker/service communication | ✅ §9 | ✅ locks, streams, groups | (none required) | COMPLETE |
| PostgreSQL security | ✅ §10 | ✅ migrations, prod host-less overlay | Backup/restore, at-rest guidance | PARTIAL |
| Redis security | ✅ §11 | ✅ internal-only prod posture | Auth/posture doc | PARTIAL |
| Market-data ingestion | ✅ §12 | ✅ provider abstraction, breaker, practice-only | (documented only) | COMPLETE |
| AI-agent risks | ✅ §13 | ✅ LLM abstraction, fallbacks, budget breaker | (documented only) | COMPLETE |
| Prompt/input/data risks | ✅ §14 | ✅ Pydantic validation, no direct LLM chat | (documented only) | COMPLETE |
| Trade-decision/orchestrator risks | ✅ §15 | ✅ no executor; gates; paper-only | (none required) | COMPLETE |
| Risk-management controls | ✅ §16 | ✅ risk knobs | (none required) | COMPLETE |
| SAFE MODE controls | ✅ §17 | ✅ L1–L5 + safety tests | Re-verify at final sign-off | COMPLETE |
| Secrets/configuration risks | ✅ §18 | ✅ .env policy, fail-closed prod key | Rotation runbook, .env.example reconcile, Grafana cred policy | PARTIAL |
| Logging & sensitive-data exposure | ✅ §19 | ✅ redaction processors | Spot-check at sign-off | COMPLETE |
| Docker/container risks | ✅ §20 | ✅ non-root prod, multi-stage; resource limits DONE | trivy, optional digest pin | PARTIAL |
| Nginx/reverse-proxy risks | ✅ §21 | ✅ headers, CSP, WS/API upgrade proxying, healthz/system passthrough | TLS + HSTS + redirect | PARTIAL |
| Dependency/supply-chain risks | ✅ §22 | ✅ ranges, lockfiles, Dependabot | pip-audit, npm audit, trivy | PARTIAL |
| Monitoring/alerting risks | ✅ §23 | ✅ healthchecks, heartbeats, exporters, staleness | Prod access posture doc | PARTIAL |
| Backup/recovery risks | ✅ §24 | ✅ volumes, runbook recovery | Backup/restore script + drill | PARTIAL |
| Availability/DoS risks | ✅ §25 | ✅ rate limits, restart policy | Document steady-state; optional later caps | PARTIAL |
| Threat scenarios T1–T14 | ✅ §26 | ✅ analyzed per control | As mapped above | – |
| Existing mitigations | ✅ §27 | ✅ COMPLETE list | – | – |
| Remaining Phase 10 mitigations M1–M9 | ✅ §28 | – | Execute in the agreed sequence (PHASE_10_AUDIT.md §“Implementation Sequence”) | IN PROGRESS |

**Acceptance criteria for closure:** M1–M9 delivered, `make verify` green, SAFE MODE probes unchanged (`mode=safe`), and this section flipped from PARTIAL/IN PROGRESS to COMPLETE via the Phase 10 hardening sign-off in `docs/runbook.md`.