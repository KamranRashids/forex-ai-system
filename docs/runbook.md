# Runbook — forex-ai-system Runtime Operations

Operational guidance for the Dockerized backend runtime: health checks,
observability access, and the recovery actions for the worker processes.

**Scope:** Phase 7 monitoring + runtime hardening, extended in Phase 10 with
backup/restore (§7) and the production overlay (TLS edge, resource limits), and in
Phase 12 with per-process worker metrics. Everything here is safe-mode
paper/analysis only (`TRADING_MODE=safe`); no live execution exists.

---

## 1. Stack Layout

| Service | Purpose |
|---------|---------|
| `postgres` | Primary DB (candles, signals, backtests) |
| `redis` | Cache, staleness keys, worker heartbeats, orchestrator lock |
| `api` | FastAPI app; `/system/status`, `/metrics`, `/docs` |
| `web` | Next.js dashboard (frontend, port 3000) |
| `worker-ingest` | Candle/trend ingest + staleness check |
| `worker-agents` | Runs technical/regime/fundamental/sentiment agents |
| `worker-content` | Slow content pipeline (300s loop, TTL 900s) |
| `worker-orchestrator` | Single-owner batch orchestrator (token-guarded lock) |
| `worker-alerts` | Durable alerts pipeline + WS feed |
| `prometheus` | Scrapes `api:8000/metrics` + per-worker exporters for runtime observability |
| `grafana` | Dashboards (Runtime Overview v0) |

All workers run `restart: unless-stopped`.

---

## 2. Health Checks

```sh
# aggregate worker health (status: up|stale|down, age, ttl per role)
curl -s http://localhost:8000/system/status | jq

# per-role heartbeats in redis (exists=1 & ttl>0 = alive)
docker exec forex-ai-redis-1 redis-cli exists worker:heartbeat:orchestrator
docker exec forex-ai-redis-1 redis-cli ttl      worker:heartbeat:orchestrator

# orchestrator lock (token present = a single owner holds it)
docker exec forex-ai-redis-1 redis-cli get lock:orchestrator
docker exec forex-ai-redis-1 redis-cli ttl lock:orchestrator
```

`/system/status` is the canonical signal. A worker is:
- **up**   — heartbeat age < declared TTL
- **stale**— heartbeat present but older than its declared TTL (loop out of sync)
- **down** — no heartbeat (not started, or cleared after graceful shutdown)

---

## 3. Observability

- **API metrics (raw Prometheus):** http://localhost:8000/metrics
  - `worker_up{role=...}` — 1 if heartbeat fresh
  - `worker_heartbeat_age_seconds{role=...}`
  - `staleness_breach_count`, `staleness_max_age_seconds`
  - `http_requests_total`, standard request duration histograms
- **Worker metrics (per-process Prometheus exporters):** ports 9101–9105
  (`worker-ingest:9101`, `worker-agents:9102`, `worker-orchestrator:9103`,
  `worker-alerts:9104`, `worker-content:9105`)
  - `forex_worker_up{worker=...}` — 1 if that worker process is alive/looping
  - `forex_worker_heartbeat_timestamp_seconds{worker=...}` — last loop pulse
  - `forex_worker_cycles_total{worker=...}` / `forex_worker_errors_total{worker=...}`
  - Container-level liveness is a separate `python /app/app/worker_healthcheck.py`
    probe per worker (see §4.4).
- **Prometheus:** http://localhost:9090 — targets → `forex-ai-api` and the
  `forex-ai-workers` job (5 worker exporters)
  - `up{job="forex-ai-api"}`; `worker_up` (all 4 roles should = 1)
  - `forex_worker_up` (all 5 workers should = 1)
- **Grafana:** http://localhost:3001 (admin / admin — dev default; rotate before wider use)
  - "Runtime Overview v0" dashboard: worker up/age, staleness, HTTP rate/latency

---

## 4. Recovery Actions

### 4.1 A worker stopped / exited
```sh
# restart a single worker (deterministic stop+start; graceful SIGTERM then boot)
docker compose restart worker-orchestrator
docker compose restart worker-ingest
docker compose restart worker-agents
docker compose restart worker-content
```
After any restart, confirm the worker returns `up` and (for the orchestrator) a
**new** lock token is held — see §2. The orchestrator lock re-acquisition retry
waits up to one lease TTL for a stale owner's lease to expire before failing
closed, so a brief `lock_pending` log is expected and correct.

### 4.2 Whole stack down / bring up
```sh
docker compose up -d
docker compose ps            # all services should be healthy/running
curl -s localhost:8000/system/status
```

### 4.3 Prometheus / Grafana broken
```sh
docker compose restart prometheus grafana
# verify: grafana datasource "Prometheus", dashboard "Runtime Overview v0"
```

### 4.4 Things that are intentional, not errors
- **`docker kill -s SIGKILL` does not auto-restart** a worker. Docker treats a
  user-initiated stop that way (documented). To recover, run
  `docker compose restart <worker>`. A genuine process crash (non-zero exit)
  **is** auto-restarted by `unless-stopped`.
- **Worker healthchecks are truthful `python` probes** — each worker container
  runs `python /app/app/worker_healthcheck.py` (exit 0 = PID 1 is a `worker_main`
  process). A "health: starting/unhealthy" status therefore reflects a real
  problem; check `docker logs <worker>` and §5. `/system/status` + the Redis
  heartbeat + `forex_worker_up` remain the runtime source of truth.
- **content worker age > 60s**: by design — its loop is 300s with declared TTL
  900s. Judge it against its declared TTL, not 60s.

---

## 5. Debugging a specific worker
```sh
docker logs --tail 200 forex-ai-worker-orchestrator-1
# look for orchestrator_batch {errors=..., processed=...} and lock_pending entries
```

---

## 6. Safe Mode Notes
- `TRADING_MODE` only accepts `safe`; there is **no executor / no live order
  path**. These recovery actions affect analysis/batch orchestration only.

---

## 7. Backup and Restore

- **Create a backup:** `make backup` (dev) or `make backup-prod` (prod overlay),
  or `./scripts/backup_db.sh [prod]`. The script uses the postgres container's own
  environment (`pg_dump`), so no credentials are embedded.
- **Output:** timestamped gzipped SQL + `.sha256` checksum under `backups/`
  (gitignored — never commit backups).
- **Restore safely (isolated):** `make restore FILE=backups/forex_ai-*.sql.gz`
  (or `./scripts/restore_db.sh backups/forex_ai-*.sql.gz [prod]`). It restores into a
  temporary `forex_ai_restore_*` database, verifies schema + Alembic state, then drops
  the temporary database. It **never** overwrites the live `forex_ai` database.
- Full procedure, storage, security, retention, and recovery guidance:
  `docs/phase10-backup-restore.md`.
