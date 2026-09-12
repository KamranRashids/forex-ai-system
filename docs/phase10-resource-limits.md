# Phase 10 — Production Resource Limits

Date: 2026-09-11. Status: **DONE (verified on the prod overlay)**. Component:
`docker-compose.prod.yml` (`deploy.resources` blocks). The dev definition in
`docker-compose.yml` is **untouched**.

## 1. Current Production Services

The prod stack is `docker compose -f docker-compose.yml -f docker-compose.prod.yml`
(or `make prod-up`): 12 services on a single pinned subnet (`172.28.0.0/16`, the
`TRUSTED_PROXIES` scope for the Nginx edge).

| Service | Image / build target | Notes |
|---|---|---|
| `api` | `backend` → `target: prod` | single uvicorn (no `--workers`), async FastAPI |
| `web` | `frontend` → `target: prod` | Next.js standalone Node server, prerendered/static routes, no `next/image` |
| `worker-ingest` | `backend` → `prod` | asyncio; synthetic provider, 7 symbols × M15/H1/H4, 10s interval |
| `worker-agents` | `backend` → `prod` | technical/regime/fundamental/sentiment analysis; `LLM_PROVIDER=none` |
| `worker-content` | `backend` → `prod` | synthetic news (300s) / calendar (3600s) polls |
| `worker-orchestrator` | `backend` → `prod` | 200ms fusion/risk loop |
| `worker-alerts` | `backend` → `prod` | alerts stream consumer |
| `postgres` | `postgres:16-alpine` | named volume `pgdata` |
| `redis` | `redis:7-alpine` | appendonly, named volume `redisdata` |
| `prometheus` | `prom/prometheus:v2.53.0` | 6 scrape targets, retention `${PROM_RETENTION:-7d}`, volume `promdata` |
| `grafana` | `grafana/grafana:11.1.1` | anonymous Viewer, provisioned datasource/dashboard |
| `nginx` | `nginx:1.27-alpine` | edge reverse proxy, volume-mounted config (ro) |

## 2. Resource Policy

- **No exact production workload data exists for this system** (SAFE MODE, demo-scale:
  one operator, synthetic providers, ~6 Prometheus targets, 7 symbols). Limits are
  therefore **conservative provisional values**, deliberately up to ~4x observed idle
  usage, so they cannot prevent startup. They are explicitly **not** benchmark-derived.
- **Limits are hard caps** (protection against one container starving/OOM-killing its
  neighbours). **Reservations are soft hints only** — on a single-host compose deploy
  they are best-effort and never prevent the engine from starting a container.
- Evidence base: live `docker stats` of the same workload (dev stack, all 5 workers +
  API + DB + metrics active) and of the prod stack itself while limited (below).
- Verified schema compatibility: both Compose files are **spec-style** (no deprecated
  `version:` key); Docker Compose v5.5.0 + Docker Engine (cgroup v2) enforces
  `deploy.resources.limits/reservations` in non-swarm mode. Confirmed via
  `docker compose config` (memory bytes: `512M`→536870912, `1024M`→1073741824,
  `256M`→268435456; `cpus`→0.5/1/2) and `docker inspect` on live containers.
- Dev (`docker-compose.yml`) intentionally unchanged; all controls live in the prod
  overlay. No `ulimits`/`pids_limit`/`stop_grace_period` added — not required for
  stability, and resource caps do not affect SIGTERM/SIGKILL shutdown behaviour.

## 3. API

| Setting | Value | Reason |
|---|---|---|
| memory limit / reservation | 512M / 128M | observed 110-170MiB (dev reload ~170MiB, prod ~110MiB); ~4x headroom for request bursts and asyncpg pool |
| cpus limit / reservation | 1.0 / 0.25 | single async uvicorn; well under 1 core at rest, headroom for burst handling |

Healthcheck (`/health/live` via python urllib) unaffected by limits.

## 4. Frontend

| Setting | Value | Reason |
|---|---|---|
| memory limit / reservation | 512M / 128M | standalone Node server observed 37-40MiB; static/prerendered routes, no `next/image` |
| cpus limit / reservation | 1.0 / 0.25 | SSR/bundled Node; 1 core headroom |

Build-time memory is **not** limited (the `builder` stage is separate and not subject to
the runtime `deploy` block). Healthcheck (Node `fetch`) unaffected.

## 5. Workers

Same profile for all five (ingest, agents, content, orchestrator, alerts):
memory 512M / 128M, cpus 1.0 / 0.25.

| Reason | Value |
|---|---|
| observed RSS 90-99MiB each (dev and prod), active loops on orchestrator (200ms) and ingest polling | 512M cap gives ~5x headroom |
| asyncio single-thread loops; occasional indicator compute (agents) and provider bursts (ingest) | 1.0 CPU cap per worker |

Worker healthchecks (Python) and heartbeat-based graceful shutdown are signal-based and
unaffected by resource caps.

## 6. PostgreSQL

| Setting | Value | Reason |
|---|---|---|
| memory limit / reservation | 1G / 256M | observed 39-60MiB; stock PG16 defaults (`shared_buffers` 128MB, `work_mem` 4MB, autovacuum) need headroom for checkpoints/sorts |
| cpus limit / reservation | 2.0 / 0.25 | autovacuum + query workers may use multiple cores |

Not tuned (no schema/config changes per scope). Cap is generous to avoid any
checkpoint/autovacuum risk.

## 7. Redis

| Setting | Value | Reason |
|---|---|---|
| memory limit / reservation | 256M / 32M | observed 52-67MiB including AOF rewrite buffering; workload is locks/tickets/heartbeats/streams |
| cpus limit / reservation | 0.5 / 0.05 | single-threaded; tiny operations |

## 8. Prometheus

| Setting | Value | Reason |
|---|---|---|
| memory limit / reservation | 256M / 32M | observed 25-32MiB with 6 targets × 15s scrapes; TSDB memory grows with series/chunks — 256M is a cap with margin for the 7d retention window |
| cpus limit / reservation | 0.5 / 0.05 | periodic scrapes + rule evaluation |

`--storage.tsdb.retention.time` unchanged (7d). If series cardinality grows in future
deployments, revisit (see Limitations).

## 9. Grafana

| Setting | Value | Reason |
|---|---|---|
| memory limit / reservation | 256M / 32M | observed 48-55MiB with one provisioned dashboard |
| cpus limit / reservation | 0.5 / 0.05 | dashboard serving is I/O-light |

## 10. Nginx

| Setting | Value | Reason |
|---|---|---|
| memory limit / reservation | 256M / 32M | observed ~11MiB; `worker_connections 1024`, static/API passthrough |
| cpus limit / reservation | 0.5 / 0.05 | proxy/WS upgrade overhead is minimal |

## 11. Verification

Executed on the prod overlay (`make prod-up` procedure) **after** validation:

1. `docker compose -f docker-compose.yml -f docker-compose.prod.yml config -q` → exit 0;
   resolved values checked (memory bytes / `cpus`).
2. Started with `up -d --build`; **all 12 containers up, every service with a
   healthcheck reported `(healthy)`** (api, web, postgres, redis, prometheus, grafana,
   and all five workers); nginx running.
3. `docker compose ps` — full output captured (12/12 running), no `Restarting`.
4. `docker inspect` per container — limits enforced on every service:
   `mem_limit`/`mem_resv`/`NanoCpus` match the table above (e.g. api 536870912/134217728/10⁹).
5. **No OOMKilled and `RestartCount=0` for all 12 services** (`docker inspect`).
6. Edge probes through Nginx: `/health/ready` → `{"status":"ok","mode":"safe"}`;
   `/healthz` → ok; `/system/status` → `app_env=prod`, `safe_mode=true`, database/redis/
   migrations all ok.
7. Live usage under caps (`docker stats`): api 109.6MiB/512MiB, web 36.7MiB/512MiB,
   workers 90-97MiB/512MiB, postgres 39.4MiB/1GiB, redis 52.0MiB/256MiB,
   prometheus 25.1MiB/256MiB, grafana 55.4MiB/256MiB, nginx 10.9MiB/256MiB — every
   service well inside its cap.
8. Prod down; dev stack restored (11/11 running).
9. Test suites re-run (same venv/deps as baseline):
   - Backend unit — **463 passed**, coverage **92.61%** (gate ≥90%).
   - Backend integration — **102 passed**.
   - Frontend — vitest **66/66 passed**, eslint clean, `tsc --noEmit` clean.
10. Container logs checked for restart/OOM/resource-limit errors — none found.

## 12. SAFE MODE Verification

Unchanged by this work (no application code touched — Compose-only change):

- `/system/status` through Nginx: `"trading_mode":"safe"`, `"safe_mode":true`,
  `app_env=prod`.
- SAFE MODE L1–L5 regression coverage (unit + `safety`-marked tests) still green in the
  463/102 suite runs above.

## 13. Acceptance Checklist

- [x] Every prod service (`api`, `web`, `worker-ingest`, `worker-agents`,
      `worker-content`, `worker-orchestrator`, `worker-alerts`, `postgres`, `redis`,
      `prometheus`, `grafana`, `nginx`) has memory + CPU limits/reservations.
- [x] Values conservative/provisional, informed by live observation, documented as such
      (not benchmark-derived).
- [x] Dev Compose untouched; all controls in `docker-compose.prod.yml`.
- [x] `docker compose -f docker-compose.yml -f docker-compose.prod.yml config` valid.
- [x] Prod stack started per project procedure; all services healthy (`docker compose ps`).
- [x] `/health/ready`, `/system/status` verified through the edge.
- [x] SAFE MODE confirmed enabled.
- [x] Backend + frontend suites green.
- [x] No restart/OOM/resource-limit errors (inspect + stats + logs).
- [x] No application/trading/agent/schema changes.

## Limitations & assumptions

- **Provisional, not benchmark-derived**: no production load data exists for this
  paper-trading, SAFE MODE demo. Values are conservative caps, not sizing guidance.
  Re-benchmark under real concurrency before any capacity claim.
- **Reservations are soft**: on a single-host non-swarm engine they are hints; only
  limits are hard guarantees. Documented, not misrepresented.
- **`ports: []` in the overlay does not remove base published ports** (Compose merges
  port lists rather than replacing them) — `api:8000`, `web:3000`, `postgres:5432`,
  `redis:6379`, `prometheus:9090`, `grafana:3001` remain published on the host when the
  overlay is used. Pre-existing behaviour; out of scope for this item (note for M1/TLS work).
- **Prometheus 256M cap**: adequate for today's small cardinality on 7d retention; a
  future expansion of scrape targets/retention should raise this cap first.
- **PostgreSQL 1G cap**: generous for stock PG16 defaults here; no tuning applied.
- **Graceful shutdown**: caps do not alter signal handling; the existing
  `exec`-as-PID-1/SIGTERM behaviour is unchanged and verified by zero unclean exits.