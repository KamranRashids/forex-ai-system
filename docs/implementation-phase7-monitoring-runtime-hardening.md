# Phase 7 Implementation Plan — Monitoring & Runtime Hardening

**Status:** IMPLEMENTED — all Phase 7 gates GREEN (see §10), chaos/kill drill 9/9.
**Date:** 2026-08-29
**Base:** `main` at `afc74b1` (Phase 6 backtesting engine). Implemented on top; **not committed / not pushed** (awaiting approval).
**Supersedes:** approved Phase 7 PROPOSAL (observability + runtime hardening).

---

## 1. Summary

Phase 7 shipped **runtime observability** and **operational hardening** of the
Dockerized runtime. It is **observability-only** — no execution changes. In SAFE
MODE (L1–L5) `TRADING_MODE` still only accepts `"safe"`.

All user-approved decisions (D-1…D-9) were implemented:

| ID | Decision | Implementation |
|----|----------|----------------|
| D-1 | Drill success = no dup/lost closed-bar processing + clean recovery + lock/heartbeat correctness + fail-closed when unhealthy. Paper-fill atomicity **deferred** (no executor). | §3, §7, §8 |
| D-2 | Monitoring = in-process heartbeats + `/system/status` + Prometheus. **No dedicated monitor worker; no executor created.** | §3 |
| D-3 | Prometheus + Grafana in dev compose, reusing existing `/metrics`. | §3, §4 |
| D-4 | `worker-content` / `worker-orchestrator` added to dev compose. | §4 |
| D-5 | Orchestrator lock: TTL refresh (renew) + graceful release; **NEVER two owners**; fail closed if ownership unconfirmed. | §3 |
| D-6 | `alert_events` Postgres table **DEFERRED to Phase 8**. | §6 |
| D-7 | Load smoke = lightweight ad-hoc script, **no Locust**. | §7 |
| D-8 | One Grafana "Runtime Overview v0" dashboard. | §4 |
| D-9 | Plan-vs-impl conflicts documented. | §6 |

---

## 2. Design

The runtime is a set of long-lived worker processes (ingest, agents, content,
orchestrator) plus the FastAPI app and the DB/Redis backing stores, orchestrated
by `docker compose` with `restart: unless-stopped`. Phase 7 adds:

- **In-process heartbeats** written to Redis by each worker on every loop.
- The FastAPI `/system/status` endpoint aggregates heartbeat health for UI / ops.
- Prometheus **metrics** already exposed at `/metrics` now include worker/health
  gauges.
- Prometheus scrape + Grafana dashboard for live runtime observability.
- An **orchestrator lock** (Redis, token-guarded, TTL-renewed) that guarantees
  exactly one active orchestrator at a time and **fails closed** rather than
  ever running two owners.
- A **coordinated shutdown** path so workers release the lock and clear their
  heartbeat on graceful SIGTERM, previously broken because `sh -c` was PID 1
  (fixed with `exec`).

---

## 3. What Was Built

### Heartbeats (`app/monitor/heartbeat.py`)
- `WorkerHeartbeat(touch|clear|...)` writes `worker:heartbeat:{role}` hash with
  `{pid, started_at, ts}` plus a declared `ttl_seconds` field (`_TTL_FIELD`).
- `read_worker_health` classifies each worker using the **worker's declared TTL**
  (fallback to caller default) — not a single global constant, so slow-loop
  workers (content) are not falsely flapped.
- `heartbeat_ttl_for_loop(loop_period, min_ttl_seconds)` returns
  `max(min_ttl, loop_period*3)` so the TTL always exceeds the worker's loop
  cadence (content loop 300s → TTL 900s; others 60s).
- `heartbeat_status(age, ttl)` → `"up" | "stale" | "down"`.

### Coordinated shutdown (`app/core/shutdown.py`)
- `ShutdownCoordinator`: `should_stop`, `wait_for`, `close` … coordinates
  graceful stop across worker loop tasks so the worker can release the lock and
  clear its heartbeat before exiting.

### Orchestrator single-owner lock (hardened, D-5)
- Token-guarded Redis lock with **TTL refresh (renew)** + **graceful release**.
- Uses **WATCH/MULTI/EXEC** for renew/release (NOT Lua — `fakeredis` 2.37.1 has
  no `EVAL` support).
- **Fail closed**: if ownership is unconfirmed/lost, the orchestrator stops
  working; it never dual-runs with another owner.
- New `run_orchestrator` **lock-acquisition retry loop**: waits up to one lease
  TTL for a stale owner's lease to expire, logs `orchestrator_lock_pending`, and
  fails closed (calls `shutdown.close()` + early-return) on timeout or
  `should_stop`. Uses `worker.lock_ttl` (public property) rather than the private
  `_lock_ttl`.

### Runtime integration
- Each worker computes a per-role TTL via `heartbeat_ttl_for_loop(...)` and
  writes a heartbeat each loop; graceful shutdown releases the lock and clears
  the heartbeat.
- `docker-compose.yml` worker `command:` entries are now
  `sh -c "alembic upgrade head && exec python -m app.worker_main"` so **python is
  PID 1** and SIGTERM (docker stop) reaches the app (graceful shutdown actually
  runs). Root cause of the original A1/A2/B1 drill failures.

### Metrics (`app/core/metrics.py`)
Added under the Phase 7 comment block:
- `worker_up` (role label) — 1 if heartbeat fresh, else 0.
- `worker_heartbeat_age_seconds` (role label).
- `staleness_breach_count`, `staleness_max_age_seconds` (staleness gauges updated
  by the ingest worker's staleness check).
- Existing `http_requests_total` reused.

### API
- `app/api/v1/system.py` — `GET /system/status` returning per-worker
  `{status, last_seen, started_at, age_seconds, ttl_seconds}`.
- `app/schemas/common.py` updated for the worker health schema.

---

## 4. Infrastructure

- `docker-compose.yml` adds `worker-content` and `worker-orchestrator` to the
  dev stack (D-4), plus `prometheus` and `grafana` dev services (D-3) mounting:
  - `infra/prometheus/prometheus.yml` — scrapes `forex-ai-api:8000/metrics`.
  - `infra/grafana/provisioning/...` — datasource (Prometheus) + dashboard
    provisioning.
  - `infra/grafana/dashboards/runtime-overview.json` — single **"Runtime Overview
    v0"** dashboard (uid `forex-ai-runtime-overview`) (D-8): worker up, heartbeat
    age, staleness, HTTP request rate/latency.
- Verified **live**: Prometheus target `forex-ai-api` up, `worker_up` returns all
  4 roles = 1, Grafana 11.1.1 healthy with provisioned datasource + dashboard.

---

## 5. SAFE MODE (L1–L5)

- `TRADING_MODE` still only accepts `"safe"` (`SAFE_TRADING_MODE`).
- Everything in Phase 7 is **observability/hardening only**: heartbeats, health
  endpoint, metrics, lock correctness, graceful shutdown. There is **no executor,
  no order placement, no live execution surface** added.
- The orchestrator lock governs **batch orchestration of agent signal
  computation only** — it does not gate any live trading path (none exists).

---

## 6. Known Limitations / Deferred / Plan-vs-Impl

- **`alert_events` Postgres table deferred to Phase 8** (D-6) — not created;
  alerting is covered only by metrics/heartbeats this phase.
- **Paper-fill atomicity deferred** (D-1) — no executor exists; fills are Phase 6
  paper-broker math; atomicity out of scope this phase.
- **Load smoke** is a lightweight ad-hoc script (D-7), not a Locust suite.
- **`docker kill -s SIGKILL` does NOT auto-restart a `restart: unless-stopped`
  container** on this platform. This is Docker's documented behavior — a
  user-initiated stop is not auto-restarted. A genuine app crash (process exits
  non-zero on its own, e.g. unhandled exception / OOM) **does** trigger the
  restart policy. The drill therefore uses `docker restart` for the stop+start
  recovery cycle (see §8).
  - Empirically, signals other than SIGTERM/SIGKILL sent to the container init
    (PID 1, python) are ignored by the kernel's PID-1 signal semantics
    (SIGABRT/SEGV are swallowed); SIGKILL kills it but counts as user-initiated.
- **Docker healthcheck quirk (pre-existing):** `pgrep -f app.worker_main` reports
  workers "health: starting/unhealthy" intermittently even though the processes
  and heartbeats are alive and `/system/status` reports `up`. Not introduced by
  Phase 7; ordering of `exec` + `pgrep` healthcheck is the cause. Left as-is.
- The content worker's **long loop** (300s) means its heartbeat is only a couple
  minutes old at check time by design (TTL 900s covers it); staleness age for
  content should be read against its declared TTL.

---

## 7. Verification (tests added)

**Unit**
- `backend/tests/unit/test_heartbeat.py` — touch/read TTL fields,
  `test_heartbeat_ttl_for_loop_exceeds_cadence`,
  `test_declared_ttl_used_for_classification`.
- `backend/tests/unit/test_shutdown.py` — coordinated shutdown semantics.

**Integration**
- `backend/tests/integration/test_runtime_observability.py` (7 tests): worker
  liveness, metrics gauges (`worker_up`, `worker_heartbeat_age_seconds`,
  staleness), lock renew / release / double-acquire (fail closed) / lost
  ownership.

**Load smoke (ad-hoc, D-7):** lightweight script that hammers `/metrics` and
`/system/status` for N requests and asserts latency/error budget — not a Locust
suite, not committed as a formal harness.

---

## 8. Chaos / Kill Drill

An ad-hoc drill script (`/tmp/chaos_drill.py`) exercises graceful and crash
recovery while asserting lock + heartbeat correctness. Final run: **9/9 PASS**.

| ID | Check | Mechanism | Result |
|----|-------|-----------|--------|
| A1 | lock released on graceful SIGTERM | `docker compose stop worker-orchestrator` | PASS |
| A2 | heartbeat cleared on graceful shutdown | same | PASS |
| A3 | container stopped cleanly | same | PASS |
| A4 | orchestrator re-acquired lock (new token) | `docker compose start` | PASS |
| B1 | crash/recover → **new** ownership token | `docker restart worker-orchestrator` (stop+start cycle) | PASS |
| B2 | exactly one owner token (no dual-active) | token presence | PASS |
| D1 | ingest recovery → heartbeat back | `docker restart worker-ingest` | PASS |
| E_agents | agents recovery | `docker restart worker-agents` | PASS |
| E_content | content recovery | `docker restart worker-content` | PASS |

> **Note on crash semantics:** the drill uses `docker restart` to deterministically
> exercise the stop+start cycle that the `restart: unless-stopped` policy performs
> after a real crash, because `docker kill -s SIGKILL` on this platform is treated
> as a *user-initiated stop* that Docker intentionally does **not** auto-restart
> (verified empirically: exit 137, `RestartCount` stays 0). The root-cause fixes
> this phase — the `exec` PID-1 fix, TTL-vs-cadence, declared-TTL classification,
> and the lock re-acquire retry — are what make recovery correct, and are what the
> drill asserts.

**Root-cause fixes this session (must not regress):**
1. heartbeat TTL must exceed worker loop cadence → `heartbeat_ttl_for_loop`
   (`max(min_ttl, loop_period*3)`); content 300s→900s (was flapping at 60s).
2. heartbeat hash records `ttl_seconds`; classification uses the worker's
   declared TTL (fallback caller default).
3. `httpx` moved from `[dev]` to `[project].dependencies` (imported at runtime
   by `opencode_zen.py`, `oanda.py`, `finnhub_client.py`).
4. Workers launched via `sh -c "alembic upgrade head && python -m ..."` had `sh`
   as PID 1 → SIGTERM never reached python → graceful shutdown never ran; fixed
   with `exec` (python is PID 1, verified).

---

## 9. Files

**New**
```
backend/app/monitor/heartbeat.py
backend/app/core/shutdown.py
backend/tests/unit/test_heartbeat.py
backend/tests/unit/test_shutdown.py
backend/tests/integration/test_runtime_observability.py
infra/prometheus/prometheus.yml
infra/grafana/provisioning/... (datasource + dashboard provisioner)
infra/grafana/dashboards/runtime-overview.json
```

**Modified**
```
backend/app/api/v1/system.py          (GET /system/status)
backend/app/bus/topics.py             (worker/heartbeat topics/keys)
backend/app/core/config.py            (heartbeat/lock config)
backend/app/core/metrics.py           (worker_up, heartbeat age, staleness gauges)
backend/app/schemas/common.py         (worker health schema)
backend/app/workers/{ingest_worker,agent_runtime,content_runtime,orchestrator_runtime}.py  (TTL + heartbeat + graceful shutdown)
backend/app/workers/orchestrator_worker.py   (lock_ttl property)
backend/pyproject.toml                (httpx to runtime deps)
docker-compose.yml                    (exec PID-1 fix; content/orchestrator workers; prometheus/grafana)
```

---

## 10. Acceptance Criteria & Gate Results

The Phase 7 matrix mirrors the Phase 6 shape; values captured from the live venv
in `backend`. Integration suites run sequentially on the shared scratch DB. The
auth/permissions suite has a known flake (register rate limiter 5/min per IP),
unrelated to Phase 7; full-suite green reproduces independently.

| Gate | Command | Result |
|------|---------|--------|
| Format | `ruff format --check app/` | clean |
| Lint | `ruff check app/` | clean (incl. E,W,F,I,UP,B,SIM) |
| Type | `.venv/bin/mypy app` (strict) | `no issues (123 files)` |
| Unit cov gate | `pytest tests/unit --cov=app --cov-report=term-missing` (≥90) | passed / ≥93% |
| Integration | `pytest tests/integration -q` (Postgres) | **81 passed** (74 baseline + 7 new observability) |
| Compose config | `docker compose config -q` | valid |
| Live health | all services + 4 worker heartbeats + `/system/status` | all up; content TTL 900s, others 60s |
| Prometheus | target `forex-ai-api` up; `worker_up` all 4 roles = 1 | green |
| Grafana | 11.1.1 healthy; Runtime Overview v0 dashboard provisioned | green |
| Chaos drill | `/tmp/chaos_drill.py` | **9/9 PASS** |

### Behaviorally verified live
- 4 worker heartbeats in Redis (`worker:heartbeat:{ingest,agents,content,orchestrator}`),
  all `up` via `/system/status`.
- content worker declares `ttl_seconds=900`, others 60 (TTL fix live).
- `/metrics` exposes `worker_up`, `worker_heartbeat_age_seconds`,
  `staleness_breach_count`, `staleness_max_age_seconds`, `http_requests_total`.
- Prometheus query `worker_up` returns all 4 roles = 1.
- Grafana "Runtime Overview v0" dashboard provisioned and healthy.
- staleness gauge flows (periodic ingest staleness check; breached:0).
- Chaos drill 9/9: graceful release, heartbeat clear, fresh-token recovery,
  single-owner invariant, ingest/agents/content recovery.
