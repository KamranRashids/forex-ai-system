# Runbook — forex-ai-system Runtime Operations

Operational guidance for the Dockerized backend runtime: health checks,
observability access, and the recovery actions for the worker processes.

**Scope:** Phase 7 monitoring + runtime hardening, extended in Phase 10 with
backup/restore (§7) and the production overlay (TLS edge, resource limits), and in
Phase 12 with per-process worker metrics. Phase 14B adds the paper-trading
observability dashboard, Prometheus alert rules (§8), and the monitoring
validation test suite. Everything here is safe-mode paper/analysis only
(`TRADING_MODE=safe`); no live execution exists.

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
  - "Paper Trading v0" dashboard (uid `forex-ai-paper-trading`, datasource
    `prometheus`): risk gates (`orch_decisions_total`, `risk_blocked_total`,
    `risk_action_requests_total`), paper lifecycle & ledger (fills, exits,
    cancelled/expired-pending orders, lifecycle cycles/errors, reconcile
    failures, snapshot health), workers & data (`worker_up`,
    `forex_worker_up`, staleness).
- **Alert rules:** `infra/prometheus/prometheus-rules.yml` (W1–W9 + W11
  active, W10 deferred + W12/W13 disabled — see §8.2). Rules are loaded from
  the file mounted at
  `/etc/prometheus/prometheus-rules.yml`; Prometheus evaluates them every
  15s. There is intentionally **no Alertmanager** and no outbound
  notification path; alerts surface in the Prometheus UI
  (http://localhost:9090/alerts) and via `ALERTS`-type queries.

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

---

## 8. Phase 14B — Paper-Trading Alert Rules & Dashboard

### 8.1 Rule inventory (W1–W9, W11 active; W10 deferred)

Thresholds below are **provisional engineering defaults**; calibrate them from
observed paper-running behavior as noted. Every rule carries a
`runbook: docs/runbook.md` annotation and a `summary`/`description` for the
intended investigation.

| # | Alert | expr / when it fires | Investigation |
|---|-------|----------------------|---------------|
| W1 | `WorkerProcessDown` | `up{job="forex-ai-workers"} == 0` (3m) — a worker exporter stopped being scraped | `docker compose ps`; `docker logs <worker>` per §4 |
| W2 | `ApiDown` | `up{job="forex-ai-api"} == 0` (3m) — API `/metrics` target down | `docker compose ps`, `docker logs forex-ai-api-1` |
| W3 | `WorkerRoleDown` | `worker_up{job="forex-ai-api"} == 0` (10m) — a Redis-heartbeat role is stale/down | `curl -s localhost:8000/system/status`; §2 heartbeats |
| W4 | `OrchestratorStalled` | `rate(orch_cycles_total[5m]) == 0 and forex_worker_up{worker="orchestrator"} == 1` (15m) — process up but scan loop not advancing | `docker logs forex-ai-worker-orchestrator-1` (lock_lost/lock_pending) |
| W5 | `PaperReconcileFailed` | `increase(paper_lifecycle_cycles_total{outcome="reconcile_failed"}[15m]) > 0` (0m) — ledger reconciliation aborted; fail-closed engaged | orchestrator logs (`paper_lifecycle_reconcile_failed`); paper order vs position state |
| W6 | `PaperLifecycleDegraded` | `increase(paper_lifecycle_errors_total[15m]) > 0` (15m) — per-unit lifecycle errors (unit skipped) | `docker logs forex-ai-worker-orchestrator-1 --tail 200` |
| W7 | `PendingExpiryBurst` | `increase(paper_pending_expired_total[15m]) > 0` (30m) — pending orders past `valid_until` (advisory; distinct from cancelled) | `docker logs forex-ai-worker-orchestrator-1 --tail 200` |
| W8 | `StalenessBreach` | `staleness_breach_count > 0` (15m) — stale (symbol,timeframe) series; may be market-closed | `curl -s localhost:8000/system/status`; confirm session calendar first |
| W9 | `Api5xxRate` | `sum(rate(http_requests_total{status=~"5.."}[5m])) > 0` (5m) — any 5xx in a 5m window | `docker compose logs api --tail 200`; traceback inspection |
| W11 | `GatePressureHigh` | `histogram_quantile(0.95, sum by (le, gate) (rate(paper_gate_used_pct_bucket[15m]))) >= 0.9` (30m) — a risk gate sat ≥90% used | `curl -s localhost:8000/system/status`; Paper Trading dashboard Row A |

### 8.2 Disabled-by-default rules (present in the file, not evaluated)

- **W10 `DecisionLatencyHigh` — DEFERRED (not an active alert).** Intended
  condition: sustained p95 orchestrator decision build/persist latency above
  **5 seconds**
  (`histogram_quantile(0.95, sum(rate(orch_decision_latency_seconds_bucket[5m])) by (le)) > 5`,
  15m, warning). It is **disabled**, not operational: the current
  `orch_decision_latency_seconds` histogram's highest explicit bucket is
  **2.0s**, so `histogram_quantile(0.95, ...)` is bounded above by 2.0 and the
  `> 5` condition is mathematically unreachable. Deploying it as a live alert
  would be a dead alert, so the definition is retained commented-out until a
  **separately approved future instrumentation change** expands the histogram
  buckets beyond 5s (e.g. `..., 2.0, 5.0, 10.0`). The threshold is **not**
  lowered. No Phase 14B application-code change is being made for this.
- **W12 `VetoChatter`** — would fire when repeated veto chatter exceeds a
  sustained threshold; kept commented out pending calibration.
- **W13 `CatchupThrottled`** — catch-up ingestion exceeding the expected
  budget; kept commented out pending calibration.

All three are present in `prometheus-rules.yml` as commented blocks deliberately.
To enable one, uncomment its rule **and** make the corresponding prerequisite
change first (for W12/W13, the `backend/app/` sentinel/metric opt-in — no app
code exists for these yet; for W10, the histogram bucket expansion) — do not
enable before that.

### 8.3 Reloading rules / config
Rules are read from the mounted file at startup; to apply rule edits without
recreating the container:

```sh
docker compose restart prometheus
docker compose ps          # prometheus should be running again
# verify no config errors (exit 0, "SUCCESS: 10 rules found"):
docker run --rm --entrypoint /bin/promtool \
  -v "$PWD/infra/prometheus:/etc/prometheus:ro" \
  prom/prometheus:v2.53.0 check rules /etc/prometheus/prometheus-rules.yml
```

### 8.4 Interfaces
- **Prometheus alerts UI:** http://localhost:9090/alerts (fire summary +
  annotations; no external delivery by design — no Alertmanager in the stack)
- **Dashboard:** Grafana → "Paper Trading v0" (uid `forex-ai-paper-trading`),
  datasource `prometheus`; three row sections: Risk Gates / Paper Lifecycle &
  Ledger / Workers & Data
- **Metric sources:** all panel/rule PromQL targets `app.core.metrics` counters
  and the per-worker exporter metrics; no metric is referenced by Phase 14B
  config that is not emitted, and `risk_paper_total` is deliberately not used
  (flagged by the monitoring validation suite)

### 8.5 Data semantics (what the numbers mean)
- `orch_decisions_total{status=...}` statuses: `analysis` (new opinion) |
  `paper` (paper action) | `blocked` (probability firewall) — see
  `DecisionStatus` in `backend/app/models/decision.py`
- `risk_blocked_total{gate=...}` gates: `exposure` | `correlation` |
  `daily_loss` | `drawdown`; `risk_blocked_total` is also the source for the
  disabled W12/VetoChatter rule
- Paper lifecycle cycle outcomes (`paper_lifecycle_cycles_total{outcome=...}`):
  `ok` | `degraded` | `reconcile_failed` (matches orchestrator batch exit
  classes); per-unit degradation is `paper_lifecycle_errors_total`
- Cancellation vs expiry are **disjoint careers**: `paper_orders_cancelled_total`
  (`superseded`/`missing_bar`) is expected control flow, never alerted; pending
  orders past `valid_until` increment `paper_pending_expired_total` and are
  raised only as W7 when their rate climbs — a single occurrence is advisory
- Exit reasons (`paper_exits_total`): `stop_loss` | `take_profit` | `signal`
- `alert.<name> == 1` sentinels (risk_brake, lifecycle/reconcile) overlap with
  the W5/W6/W11 rule surfaces; either surface green implies recovery

### 8.6 Validation
- `backend/tests/unit/test_monitoring_config.py` — the monitoring validation
  suite: asserts every rule/panel metric token is defined and live (with the
  middleware-local exception), dashboard JSON/UID/datasource, rule structure and
  threshold format, W1/W2 exact expressions, `up{job}` vs scraped jobs, no
  dead-metric references, no outbound receiver or credential placeholders, and
  W10/W12/W13 remain disabled (W10 retaining its intended `> 5` threshold).
  Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_monitoring_config.py -v`

### 8.7 Known limitations
- No Alertmanager / no outbound delivery: Prometheus is the alert surface.
- W10 (`DecisionLatencyHigh`) is **DISABLED** (deferred). With the current
  histogram cap at 2s its `> 5s` condition cannot fire; activation requires a
  separate approved instrumentation change to extend the
  `orch_decision_latency_seconds` buckets beyond 5s.
- W8 (`PaperExitsSharpDrop`) is intentionally tolerant of market-closed hours —
  judge drops against whether the market was active (session calendar) before
  treating as an alert.
- Sentinel-based rules (`alert.risk_brake`, lifecycle/reconcile) overlap with
  `orch_lifecycle_*` rates; either surface being green implies recovery.
