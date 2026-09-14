# Next Milestone Audit

> Status: read-only audit + planning artifact. Created 2026-09-12 against the released
> `v0.1.0` tag (`d28af88`). No project files were modified; this document is the only
> artifact produced. All statements below are grounded in the inspected repository.

---

## 1. Current v0.1.0 baseline

- **Product:** multi-agent Forex *analysis* system running exclusively in paper/backtest
  mode. Live order execution does not exist anywhere in the codebase (`docs/safe-mode.md`).
- **Phases delivered:** 0–10 accepted (`docs/phase10-final-audit.md`), Phase 11 release
  prep done, Phase 12 (observability + candles/latest-prices API + per-process exporters)
  done.
- **Stack (verified):** Python 3.12 · FastAPI · SQLAlchemy 2 async · PostgreSQL 16 ·
  Redis 7 (streams/pub-sub/cache) · Next.js 15.5.25 (App Router) · React 19 · TypeScript ·
  Tailwind v4 · Docker Compose · NGINX prod overlay.
- **Runtime:** 11 compose services (postgres, redis, api, web, 5 workers
  ingest/agents/content/orchestrator/alerts, prometheus, grafana) — all healthy;
  `/health/live` + `/health/ready` = `{"status":"ok","mode":"safe"}`; migration head `0007`.
- **Gate status:** unit 463 @ 92.61%, integration 102, frontend vitest 66/66; ruff/mypy/
  eslint/tsc/next-build clean; pip-audit 0; npm-audit gate green; Trivy digest-pinned.

## 2. Git/release state

- Branch `main` = `d28af88` `release: v0.1.0`; **annotated tag `v0.1.0` exists**; working
  tree **clean**; `origin/main` up to date; 20 commits total (`78b780b` init → `d28af88`).
- CHANGELOG `[0.1.0] - 2026-09-12` exists; frontend/backend versions both `0.1.0`.
- **Doc drift noted (do not “fix” silently in this audit):** `PROJECT_STATUS.md` §1/§6
  and `README.md` still describe Phase 11 “in progress” and tag/publication as “FUTURE”,
  i.e. they predate the tag being created. They are historical enough to be misleading —
  recommend a small docs pass in the next milestone (not part of this audit's file set).

## 3. Implemented components (complete and working)

| Component | Where | Notes |
|---|---|---|
| Config/SAFE MODE L1 | `app/core/config.py`, `constants.py` | `TRADING_MODE` only accepts `safe`; fail-fast validation |
| Auth/RBAC | `app/services/auth_service.py`, `app/core/security.py`, `app/api/v1/auth.py`, `users.py` | Argon2id, JWT access + rotating refresh w/ reuse detection; admin/viewer roles; first user admin |
| Rate limiting | `app/core/ratelimit.py` | Per-IP, honors trusted proxies |
| Market data | `app/data/ingest.py`, `providers/` (oanda, synthetic), `market_hours.py`, `market_config.py` | Provider-independent (ADR-0003); candles upserted to PG; `bars.closed.{tf}` streams; latest-price cache; staleness alerts; admin backfill queue |
| Content + news/calendar | `app/data/providers/*_content.py`, `content_repository.py` | Finnhub + synthetic adapters; dedup; synthetic default |
| Agents (technical, regime, fundamental, sentiment) | `app/agents/*`, `app/workers/agent_runtime.py`, `worker_worker.py` | `BaseAgent.analyze → AgentSignal`; idempotent persistence (`agent_signals`); publish `signals.stream` |
| Decision pipeline | `app/decisions/engine.py`, `fusion.py`, `hashing.py` | Pure `compute_decision` + DB-aware `DecisionEngine.decide`; fuse → context → coverage → risk → ANALYSIS/PAPER/BLOCKED; cooldown; input hashes |
| Risk engine | `app/decisions/risk.py`, `app/data/risk_config.py` | ATR-based paper sizing (units, SL, TP, RR, risk %) + 4 fail-closed gates (RR/exposure/correlation/daily-loss/drawdown); persists `risk_evaluations` + `risk_state` |
| Backtesting engine | `app/backtest/*` | Deterministic replay (driver/agent_runner/orchestrator_sim), metrics (net/gross PnL, profit factor, Sharpe, Sortino, max DD, exposure), `backtest_runs/trades/equity`, REST + CLI |
| Paper broker primitives | `app/broker/paper.py`, `positions.py`, `costs.py` | Cash-based in-memory simulator + Position/PositionSet + spread/slippage costs |
| Orchestrator worker | `app/workers/orchestrator_runtime.py`, `orchestrator_worker.py` | Single-owner token-guarded lock; cycle poll + periodic rescan; persists + publishes decisions |
| Alerts | `app/workers/alert_runtime.py`, `app/alerts/translate.py`, `api/v1/alerts.py` | Durable `alerts.stream` → `alert_events`; admin ack; severities |
| WebSocket | `app/ws/hub.py`, `tickets.py`, `api/v1/realtime.py` | One-time ticket auth; topics `alerts`/`signals`/`decisions`; `fills` **reserved** (blocks 4403) |
| REST API | `api/v1/*` (system, auth, users, admin, signals, content, decisions, risk, market, backtests, alerts, realtime) | RBAC-gated incl. Phase 12 candles/latest-prices |
| Observability | `app/core/metrics.py`, `app/monitor/*`, `infra/prometheus/`, `infra/grafana/` | API `/metrics`; worker exporters 9101–9105; Prometheus scrape; Runtime Overview dashboard |
| Infra/ops | `docker-compose*.yml`, `infra/nginx/`, `scripts/{backup,restore}_db.sh`, `Makefile` | Prod TLS edge (staging self-signed), resource limits, backup/restore with isolated temp-DB drill |
| CI/Security | `.github/workflows/*`, `.trivyignore`, `check-npm-audit.cjs` | ruff/mypy/tests/coverage, pip-audit, npm-audit, Trivy; SHA-pinned; Dependabot |

## 4. Partially implemented components

| Component | What exists | What is missing |
|---|---|---|
| **Paper trading loop** | PAPER decision intents emitted risk-gated and stored; exposure derived from *active PAPER decisions* (`decision_repository.load_active_paper_snapshot`, refs at `app/decisions/engine.py:265`, `:505`) | No consumer of intents, **no executor worker**, no fills, no ledger |
| Broker abstraction (L2) | `docs/safe-mode.md:15` describes a `BrokerAdapter` interface | **No `BrokerAdapter` symbol exists** in `app/broker/` (grep: 0 hits) — only the concrete `PaperBroker`. Introduce a real `Protocol` when the ledger work begins |
| Frontend charting | Equity/drawdown curves as hand-built SVG (`frontend/src/lib/backtests.ts` `buildPath`/`buildEquityPath`, rendered in `backtests/page.tsx`) | No charting library; candles are a plain HTML table (`signals/page.tsx`), no candlesticks, no axes/tooltips |
| Timeframes | M15/H1/H4 active | M5/D1 support exists but config-gated off |
| Trade metrics | `orch_decisions_total`, `risk_blocked_total{gate}`, `risk_paper_total` | No fills/equity/positions metrics |
| Redis publishes | `bars.closed.*`, `signals.stream`, `decisions.stream`, `alerts.stream`, `prices.live` pub-sub | `events.fills` (planned) absent |

## 5. Deferred components (by design, not bugs)

- **Paper-executor loop** — orchestrator emits fully risk-gated PAPER intents, no worker
  consumes them. Original target was “Phase 5” (`IMPLEMENTATION_PLAN.md` §14 line 658);
  `worker_main.py` still logs `worker_role_not_available_yet … arrives_in Phase 5+`.
- Tables `orders_paper`, `positions`, `account_snapshots` — planned `IMPLEMENTATION_PLAN.md`
  §9 (lines 500–502), **do not exist** (only docstring/plan references).
- `events.fills` bus topic + WS `fills` topic (`hub.py:14` reserved; `TRADE_INTENT` plan §3.4).
- Frontend portfolio/positions/equity page and a charts page.
- Live trading (v1 non-goal); real-CA TLS/HSTS (blocked on production domain); external
  market/LLM providers unconfigured (`MARKET_DATA_PROVIDER=synthetic`, `LLM_PROVIDER=none`
  default; zero-key exits — optional, never required).

## 6. Current multi-agent execution flow

```
synthetic/oanda provider (default: synthetic)
   │  fetch candles (per TF)
   ▼
ingest worker ──▶ candles upserted to PG  (instruments, candles)
   │              bars.closed.{tf} streams, latest-price keys, staleness
   ▼
agent workers (technical · regime · fundamental · sentiment)
   │  analyze each closed bar  →  agent_signals (idempotent)
   │                              signals.stream
   ▼
orchestrator worker  (single-owner token lock)
   │  DecisionEngine.decide(symbol, timeframe):
   │    load latest signals @ bucket → fuse() (+ parent-TF context)
   │    coverage >= min → risk.assess()  [4 gates + ATR sizing, fail-closed]
   │    cooldown check → status = ANALYSIS | PAPER | BLOCKED
   ▼
decisions + risk_evaluations persisted (idempotent on symbol/timeframe/bucket)
   │  decisions.stream  →  API WS → frontend (badge/observe only)
   ▼
risk_state  (account/daily realized loss, peak equity, drawdown, exposure
             recomputed from active PAPER decisions, NOT a position ledger)
   ▼
   [ NO EXECUTION STEP ]
   A PAPER decision is terminal: it is an "intent". Nothing fills it.
```

Key files: `workers/orchestrator_runtime.py`, `workers/orchestrator_worker.py`,
`decisions/engine.py`, `decisions/risk.py`, `data/decision_repository.py`.

## 7. Current backtesting capability

- `app/backtest/driver.py` reuses the **same agent code path as live** (G5 principle):
  signals → fusion → risk → `PaperBroker` fills.
- Deterministic: seeded, bar-driven; orders generated at bar close, filled at **next bar
  open**; SL/TP evaluated on subsequent bar closes only (no look-ahead, decision D-C).
- Fills apply spread+slippage (`app/broker/costs.py`); tracks cash/equity/peak-equity/max
  drawdown/positions/trades (`PaperBroker.state`).
- Persisted: `backtest_runs` (frozen config), `backtest_trades`, `backtest_equity`.
- Report metrics: net/gross PnL, total costs, trades, win rate, profit factor, Sharpe,
  Sortino, max drawdown, avg exposure, bars, degraded runs. REST:
  `/api/v1/backtests{,/id,/trades,/equity}`; CLI reporters in `app/backtest/report.py`.
- SAFE MODE: `test_backtest_safe_mode.py` asserts `PaperBroker` has no DB execution surface.

## 8. Current paper-trading capability (as of v0.1.0)

- **Intents only.** `DecisionStatus.PAPER` = fully risk-validated paper intent, stored and
  streamed; exposure/position accounting is *approximated* from active PAPER decisions
  (`decision_repository.load_active_paper_snapshot`, explicitly documented as the pre-ledger
  source, `decision_repository.py:226-252`).
- `PaperBroker` (fills/costs/equity) exists but is **exercised only by the backtester**
  (`backtest/service.py:97`, `driver.py`); it is synchronous and in-memory, no DB session.
- No executor worker, no orders/positions/account tables, no fills events/topic, no
  portfolio API, no portfolio UI. The `executor` worker role is refused
  (`worker_main.py`).

## 9. Current frontend capability

| Page | What it shows |
|---|---|
| `/` | Static landing: SAFE MODE badge + nav (no data) |
| `/signals` | Live agent signals, fused decision (status PAPER/BLOCKED/ANALYSIS), recent candles **table**, latest risk evaluation (position size/SL/TP/RR/gates), signal + decision history; WS `signals`,`decisions` |
| `/backtests` | Runs list (net PnL, #trades, win rate), 12-cell metrics grid, coverage table, **SVG equity + drawdown curves**, trades table, multi-run %-normalized overlay |
| `/alerts` | Live alert feed (REST history + WS), severity cards, ack/new badges; WS `alerts` |
| `/login` | Login/register |

- Libs: `auth.ts` (sessionStorage bearer, `authFetch`), `system.ts`, `market.ts`,
  `signals.ts` (+ `SignalsSocket`), `backtests.ts` (formatters + SVG path builders),
  `alerts.ts` (+ `AlertsSocket`).
- **No** portfolio/positions/balance/P&L/equity (live) UI anywhere. **No charting library** —
  only bespoke SVG polylines for backtest equity; candles are a table. WS topics used:
  exactly `signals`, `decisions`, `alerts`. No state/data-fetching library (plain hooks).

## 10. Current database capability

Tables in the released schema (`models/*` + migrations 0001–0007), 17 total:

`users`, `refresh_tokens`, `instruments`, `candles`, `agent_signals`,
`news_items`, `economic_events`, `audit_log`, `decisions`, `risk_evaluations`,
`risk_state`, `backtest_runs`, `backtest_trades`, `backtest_equity`,
`alert_events`, `system_settings`, `provider_health`.

**Answering the audit's Q9 directly:** for orders / positions / trades / balances / P&L /
paper accounts / execution events there are **zero** tables. Closest existing surfaces:
- risk sizing + vetoes → `risk_evaluations` (position_size_units, stop_loss, take_profit,
  rr_ratio, risk_pct_account, gate booleans)
- rolling brakes → `risk_state` (realized_loss, peak_equity, max_drawdown, exposure,
  scope/period_key)
- simulated closed trades (backtest only) → `backtest_trades` and equity → `backtest_equity`
- intents → `decisions` (status=PAPER)
- planned-but-absent (plan §9): `orders_paper`, `positions`, `account_snapshots`.

## 11. Current API/WebSocket capability

REST (all under `/api/v1`, RBAC-gated): `system`, `auth` (register/login/me/logout/token
refresh), `users`, `admin` (market data admin/backfill), `signals` (latest/history),
`content` (news, calendar), `decisions` (latest/history/`risk-evaluations`), `risk` (params
read), `market` (candles, latest prices), `backtests` (list/detail/trades/equity), `alerts`
(history, ack), plus `/health/live`, `/health/ready`, `/system/status`, `/metrics`, `/docs`.

WebSocket: `POST /api/v1/ws/ticket` (one-time) → `GET /api/v1/ws/stream?ticket=…` → subscribe
`{"type":"subscribe","topics":[…]}`; allowed topics `alerts`,`signals`,`decisions`; `fills`
reserved/unsubscribable (4403). Socket is read/observe-only.

**For trading simulation, nothing exists today:** no order/position/account endpoints and
no fills WS events.

## 12. Existing test coverage (relevant areas)

- Unit (387 `def test_`; 463 executed cases @92.61% gate): `test_broker_paper.py`,
  `test_broker_positions.py`, `test_broker_costs.py`, `test_backtest_driver.py`,
  `test_backtest_report.py`, `test_backtest_safe_mode.py`, `test_decision_engine_pure.py`,
  `test_decision_risk.py`, `test_decision_fusion.py`, `test_decision_safety.py`,
  `test_ws_hub.py`, `test_ws_tickets.py`, `test_ws_origin.py`, `test_config_safe_mode.py`
  (every non-safe mode rejected), `test_risk_config_pure.py`, plus agent/market/content/
  auth/security/ratelimit/monitoring suites.
- Integration (100 `def test_`; 102 executed): `test_backtest.py`, `test_decision_pipeline.py`,
  `test_agent_pipeline.py`, `test_alerts_pipeline.py`, `test_auth_flow.py`, `test_token_rotation.py`,
  `test_permissions_matrix.py`, `test_market_api.py`, `test_content_pipeline.py`,
  `test_ingest_pipeline.py`, `test_runtime_observability.py`, `test_system_health.py`.
- Frontend vitest 66: `alerts.test.ts`, `backtests.test.ts`, `market.test.ts`, `signals.test.ts`.
- Gap: **no tests** for fills simulation, order lifecycle, position ledger, account
  snapshotting, or portfolio APIs (they don't exist yet).

## 13. Missing functionality (for SAFE paper trading)

1. **Ledger schema:** `orders_paper`, `positions`, `account_snapshots` (plan §9) — migration `0008`.
2. **Executor worker:** `WORKER_ROLE=executor` (new `elif` in `worker_main.py`, keep else →
   refuse), compose service, consumer of PAPER intents (new group on `decisions.stream` or
   direct `TRADE_INTENT`-style event per plan §3.4), next-bar-open fill + SL/TP evaluation
   per closed bar (reuse PaperBroker fill/cost logic).
3. **Async/persistent ledger:** PaperBroker today is sync + in-memory; add a DB-backed
   ledger layer reusing its deterministic fill math — do **not** rewrite the maths.
4. **`events.fills` bus topic + WS `fills` topic** (un-reserve in `hub.py`).
5. **REST portfolio API:** account summary, open positions, paper orders, P&L, equity
   snapshots (RBAC).
6. **Frontend portfolio page:** positions/P&L/equity — reuse SVG-path pattern from
   `backtests.ts`; optionally add a real chart later.
7. **Risk-gate alignment:** switch exposure/correlation/daily-loss/drawdown inputs from the
   PAPER-decision-derived snapshot to the **real open-position ledger**, keeping fail-closed
   semantics; reconcile `risk_state`.
8. **BrokerAdapter Protocol** (L2): formalize so SAFE MODE review is structural.
9. **Metrics:** fills total, open positions, equity, pnl; alerting on executor health.
10. **Observability/ops:** executor heartbeat/healthcheck/backoff, runbook §, tests
    (incl. integration fill simulation against synthetic feed), docs, SAFE-MODE regression
    updates.

## 14. Risks and dependencies

- **SAFE MODE integrity (highest risk):** adding an execution-ish worker must never open a
  path to a live broker. Mitigation: paper-only executor, single `BrokerAdapter` impl,
  `worker_main` refuses unknown roles (unchanged), SAFE-MODE suite extended first, kill
  switch unchanged (`make dev-down`).
- **Duplicate-work trap:** PaperBroker + cost model already exist and are well-tested.
  Extend, don't rebuild. Same for decision/risk pipeline and WS hub.
- **Gate-input migration:** changing exposure source (decisions → ledger) alters
  `_gate_state` (`engine.py:257-279`) and `_refresh_exposure`; must keep fail-closed and
  ideally produce identical decisions when ledger ≈ PAPER-snapshot (G5 reproducibility).
- **Determinism/no-look-ahead:** fills depend on `candles` completeness + FX market hours
  (weekly model) + cooldowns; document that live-paper runs at bar-open per closed candle.
- **Correlation baskets:** currently derived from symbol strings (`{sym[:3]},{sym[3:]}`) in
  `load_active_paper_snapshot`; the ledger should persist the basket/currency metadata for
  consistency (`instruments` would need richer columns if extended).
- **Stream/lifecycle:** decisions.stream consumer group + retention; idempotent fills
  (decision_id uniqueness); replay safety on worker restart (mirror orchestrator's
  lock/heartbeat pattern).
- **Backlog dependency:** no external providers required (synthetic default), no new
  runtime deps expected; hashed lockfiles must be regenerated if a chart lib is added.
- **Coverage gate:** unit coverage must stay ≥90% on the new sync core; integration suite
  needs real PG/Redis (already provisioned in CI).

## 15. Recommended next milestone

**Phase 13 — PAPER TRADING ENGINE (SAFE MODE paper only).** Primary candidate approved with
the above gaps as scope: a persistent, observable paper-trading loop that turns risk-gated
PAPER decisions into simulated fills, positions, account/equity and P&L — with the backtester
and live-paper loop sharing the exact same signal→risk→fill path (G5). No live-broker surface
anywhere; all work stays inside the existing SAFE MODE contract.

## 16. Proposed implementation phases

- **A. Schema (migration `0008`):** `orders_paper` (id, decision_id FK→decisions, symbol,
  timeframe, side, units, entry/exit price, status, costs JSONB, timestamps), `positions`
  (symbol/tf, side, units, entry, sl/tp, basket, status), `account_snapshots` (ts, balance,
  equity, open_pnl, realized_pnl, drawdown). Follow existing models/migration conventions.
- **B. Ledger service:** `app/broker/ledger.py` (or executor/) — async DB-backed service that
  reuses PaperBroker fill/cost math for order→position→trade transitions; add `BrokerAdapter`
  Protocol; idempotent fills keyed by decision/order; unit-test pure parts first.
- **C. Executor worker:** `WORKER_ROLE=executor` branch + `executor_runtime.py` (+ compose
  service, heartbeat, healthcheck, resource limits); consume PAPER intents, fill at next bar
  open, evaluate SL/TP per closed bar, close on opposite/flat signal; reconcile risk_state;
  publish `events.fills`.
- **D. API + WS:** account/positions/orders/equity endpoints (RBAC), `fills` WS topic
  (un-reserve), ticket access mapping, /system/status awareness of executor.
- **E. Frontend:** Portfolio page (account summary, open positions, P&L, fills feed, live
  equity curve reusing SVG pattern); extend nav/auth-guard pattern.
- **F. Observability + tests + docs:** metrics, runbook, threat-model re-review, SAFE-MODE
  suite extension, integration tests (fill simulation vs synthetic feed, restart/replay,
  idempotency), coverage maintained, CHANGELOG entry.

Sequencing note: A→B have no runtime risk and produce the ledger; C is the only new process;
D/E are consumers. F runs throughout/produces release readiness.

## 17. Acceptance criteria

1. A risk-gated PAPER decision becomes a **persisted paper order → filled position** at the
   next bar open with the same fill/cost math as the backtester (deterministic, seeded).
2. Positions mark-to-market each closed bar; SL/TP/exits close trades; closed trades feed
   realized P&L → `account_snapshots` and update `risk_state`.
3. Exposure/correlation/daily-loss/drawdown gates read from the **actual ledger** (fail
   closed when empty/unavailable) and reproduce prior PAPER-decision behavior before the
   ledger (regression parity check).
4. Idempotency: replay/restart produces **no duplicate fills or positions**; decision_id →
   one order.
5. `fills` WS topic + portfolio REST contract readable by an authenticated viewer;
   admins only for destructive ops; permission matrix updated.
6. SAFE MODE: L1–L5 regression suite extended and green; `worker_main` still refuses any
   non-listed role; zero code paths can reach a live broker (single BrokerAdapter impl).
7. Gates: unit ≥90% coverage, full integration suite (incl. executor fill sim) passes,
   ruff/mypy/eslint/tsc/next-build clean, vulnerability gates green.
8. Live system unaffected: existing 11 services healthy, `/system/status` green, no changes
   to trading-mode config, no regressions in existing 463/102/66 tests.

## 18. SAFE MODE constraints

- The executor is a **paper** executor: simulated fills only, no orders leave the database,
  no broker/library that could route anywhere.
- `TRADING_MODE` remains `safe`-only (L1); `worker_main` unknown-role refusal remains (L2+).
- The single BrokerAdapter implementation stays `PaperBroker`-based until a new ADR + owner
  approval changes the contract (`docs/safe-mode.md` escalation policy).
- No real-money, no automatic real order execution, no margin/financing, no market orders
  against a live venue. Kill switch remains `make dev-down`.

## 19. Explicitly out-of-scope items

- Live/real-money trading, brokerage integrations (OANDA *live*, PrimeXBT, etc.), any order
  routing beyond the persisted paper ledger.
- Real-CA TLS / HSTS (blocked on production domain — independent track).
- Activating M5/D1 timeframes, adding new data providers, LLM tuning.
- Advanced order types (limit, trailing, partial fills), financing/margin, multi-user
  accounts, portfolio rebalancing strategies.
- Charting library upgrade (nice-to-have; SVG reuse is sufficient for v0.1 scale).

## 20. Recommended first implementation task

**Phase A: migration `0008` + `BrokerAdapter` Protocol + async ledger service + pure unit
tests — no new worker, no API/UI changes yet.**

- Migration `0008` creating `orders_paper`, `positions`, `account_snapshots` following the
  existing model conventions (UUID PKs, Numeric(20,6)/Numeric(18,8), JSONB, unique keys,
  indexes, CheckConstraints).
- `app/broker/adapter.py` defining the `BrokerAdapter` Protocol (open/close/mark/snapshot)
  implemented by a new async, DB-backed `LedgerBroker` that **delegates fill/cost math to the
  existing `PaperBroker`/`CostParams`** (no duplicated simulation logic).
- Unit tests: ledger open/close/idempotency, snapshotting, P&L, SAFe-MODE no-execution-surface
  assertions (mirroring `test_backtest_safe_mode.py`), parity of ledger vs PaperBroker maths.
- This is the smallest vertical slice with no runtime behavior change; Phase C (executor
  worker) builds directly on it.

---

## AUDIT COMPLETE

### Exact files inspected (read-only)
- Git: status (`clean`), branch (`main`), `git log` (`d28af88`, tag `v0.1.0`), `git ls-files`.
- Docs: `README.md`, `IMPLEMENTATION_PLAN.md` (headings + executor/paper-trade refs),
  `PROJECT_STATUS.md`, `docs/architecture.md`, `docs/safe-mode.md`, `docs/runbook.md`
  (via exploration), `CHANGELOG.md`, `docs/phase10-final-audit.md` (via exploration).
- Backend: `worker_main.py`, `workers/orchestrator_runtime.py`, `workers/orchestrator_worker.py`
  (via exploration), `decisions/engine.py`, `decisions/risk.py`, `data/decision_repository.py`,
  `broker/paper.py`, `broker/__init__.py`, `broker/positions.py`/`costs.py` (via exploration),
  `models/decision.py`, `models/risk_state.py`, `models/risk_evaluation.py`, `models/backtest.py`,
  `models/instrument.py`, all `models/*.py` (table list), `migrations/versions/0001–0007`,
  `api/v1/decisions.py`, `api/v1/backtests.py`, `api/v1/realtime.py`, `ws/hub.py`, `ws/tickets.py`,
  `core/config.py`, `core/metrics.py` (via exploration), `main.py` (router list), `backtest/*`
  (via exploration).
- Frontend: all pages (`page/`, `signals/`, `backtests/`, `alerts/`, `login/`), all libs
  (`auth/config/market/signals/backtests/alerts/system`), `package.json` (via exploration).
- Infra/tests: `docker-compose.yml`, `docker-compose.prod.yml`, `Makefile` (via exploration),
  `test_*` inventory (387 unit + 100 integration defs), `monitor/*`, `infra/prometheus/` (via exploration).

### Exact files created
- `docs/next-milestone-audit.md` (this document).

### Exact files modified
- None. (`PROJECT_STATUS.md` and `README.md` were inspected but NOT touched; their Phase 11
  “in progress” wording is noted as drift in §2.)

### Whether anything was changed
- The project itself was **not modified**: no code, config, Docker, schema, CI, or docs
  edited; nothing installed; nothing committed/tagged/pushed. The only artifact written is
  the new read-only planning document above.

### Recommended next OpenCode command
- After approval, begin Phase A — e.g.:
  `opencode` → *“Implement Phase A of the next milestone (per docs/next-milestone-audit.md §16): migration 0008 (orders_paper, positions, account_snapshots) + BrokerAdapter Protocol + async LedgerBroker plus pure unit tests. Do not add the executor worker or any API/UI changes yet.”*
- (Sanity check first, if desired: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/projects/forex-ai-system && git status --short"` should show only `?? docs/next-milestone-audit.md`.)

STOP — awaiting approval.