# Phase 13C Wiring Audit: PAPER Decision → LedgerBroker

> **Audit-only.** This document analyses how `PAPER` decisions should reach the
> `LedgerBroker` for live paper execution. No code changes are made; the only
> file created is this audit.

---

## 1. Current Decision Flow (as of v0.1.0)

```
signal.emitted (Redis stream)
        │
        ▼
OrchestratorWorker.poll_once()          ← consumer group "orchestrators"
        │
        ▼
DecisionEngine.decide(symbol, timeframe)
   ├─ load signals from DB
   ├─ compute coverage, regime, atr, price
   ├─ load GateState (exposure, correlation, daily_loss, drawdown)
   ├─ load parent context from larger TFs
   ├─ compute_decision(inputs, orch, risk)   ← pure, synchronous
   │    ├─ fuse signals → FusedResult
   │    ├─ apply parent context
   │    ├─ check coverage, agreement, FLAT
   │    └─ assess risk → RiskOutcome (4 gates + sizing)
   ├─ apply cooldown suppression
   ├─ save_decision(session, values)          ← first-writer-wins on (symbol, tf, bucket_ts)
   ├─ save_risk_evaluation(session, ...)      ← linked by decision_id
   ├─ refresh_exposure(session)
   └─ return DecideResult
        │
        ▼
OrchestratorWorker.process_pair()
   ├─ if created + BLOCKED → emit alert.risk_brake
   └─ if created → publish decision.emitted to decisions.stream   ← PUBLISHED BUT UNCONSUMED
```

**Gap:** `decisions.stream` is published to (`RedisEventPublisher.publish_decision`) but
nothing reads it. PAPER decisions are persisted to the `decisions` table but never
reach `LedgerBroker.open_at_next_open()`. The `orders_paper` and `positions` tables
remain empty in production.

---

## 2. What PAPER Means Today

A decision reaches `PAPER` status **only** when all of these hold:

| Gate | Condition | Fail-closed |
|------|-----------|-------------|
| Coverage | `>= coverage_min` (0.5) | Low coverage → SKIP (not persisted) |
| Agreement | `>= agreement_min` (0.5) | Low agreement → ANALYSIS (persisted, not promoted) |
| Direction | Not FLAT | FLAT → SKIP |
| Risk enabled | `risk_enabled = True` | Disabled → ANALYSIS |
| Exposure | `exposure_used + risk_pct <= max_exposure_pct` | Over → BLOCKED |
| Correlation | `!triggered OR basket + risk_pct <= cap` | Over → BLOCKED |
| Daily loss | `daily_loss + risk_pct <= max_daily_loss_pct` | Over → BLOCKED |
| Drawdown | `drawdown + risk_pct <= max_drawdown_pct` | Over → BLOCKED |
| Cooldown | No non-FLAT decision within `cooldown_seconds` | Recent → ANALYSIS |
| Sizing | ATR > 0, price > 0 | Missing → BLOCKED |

When **all** gates pass and `risk_enabled` is true, the outcome is `DecisionStatus.PAPER`.
The risk evaluation already computed: `position_size_units`, `stop_loss`, `take_profit`,
`rr_ratio`, `atr`, `price`.

---

## 3. LedgerBroker Surface (Phase A)

`LedgerBroker.open_at_next_open()` is the single entry point:

```python
async def open_at_next_open(
    self, *, symbol, timeframe, direction, ref_price, ts, units,
    decision_id=None, stop_loss=None, take_profit=None
) -> PaperOrderRow | None
```

- Returns `None` if `direction == FLAT` or `units <= 0` (guard).
- Returns `None` if a position is already open for the symbol (one-position-per-symbol).
- Delegates fill math to `PaperBroker.enter_at_next_open()` (deterministic, same engine as backtests).
- Persists `PaperOrderRow` (status=FILLED) + `PaperPositionRow` (status=OPEN) via `LedgerStore`.
- The order carries `decision_id` as FK (SET NULL, unique constraint on decision_id).

**SAFE MODE:** This is the only `BrokerAdapter` implementation. There is no live-execution
path. The `PENDING` order status exists for a future executor but `LedgerBroker` fills
immediately at decision time (next-bar open semantics baked into PaperBroker).

---

## 4. Design Decision: Where to Wire

### Option A — Inline in OrchestratorWorker (Recommended)

After `DecisionEngine.decide()` returns a `DecideResult` with `status == PAPER` and
`created == True`, immediately call `LedgerBroker.open_at_next_open()` **within the
same async flow**, before publishing `decision.emitted`.

**Pros:**
- No new worker role needed (`executor` is explicitly refused in `worker_main.py:63`).
- Atomic: decision save + order/position save share the same DB transaction.
- Simpler: fewer moving parts, no new consumer group, no new Redis stream.
- The orchestrator already holds the `AsyncSession` and has access to settings/config.
- Idempotency is free: first-writer-wins on `save_decision()` + unique `decision_id` on
  `orders_paper` + one-OPEN-per-symbol constraint.

**Cons:**
- Couples decision logic and execution logic in one worker.
- If `open_at_next_open()` fails, the entire `process_pair` must handle it gracefully
  (order rejection is not a pipeline crash).

### Option B — Separate Executor Consumer (Deferred to 13D+)

Subscribe to `decisions.stream`, filter for `status == PAPER`, call `LedgerBroker`.

**Pros:** Clean separation of concerns. Executor can be independently scaled/restarted.

**Cons:** Requires a new worker role (`executor`), new consumer group, new Redis stream
consumer, separate transaction boundary (two-phase: save decision → commit → consume →
save order). Adds significant complexity for paper-only operation.

**Recommendation:** Implement Option A now (13C). Option B is appropriate for a future
phase when live-execution is considered (which would require a new ADR superseding
ADR-0002 and owner approval per the SAFE MODE escalation policy).

---

## 5. Detailed Integration Point (Option A)

The wiring goes into `OrchestratorWorker.process_pair()` in `app/workers/orchestrator_worker.py`.

### 5.1 What Changes

**`app/workers/orchestrator_worker.py`** — the only production file to modify:

1. **Inject `LedgerBroker`** into `OrchestratorWorker.__init__()` (optional, defaults to `None`).
2. **After `engine.decide()` returns** with `created == True` and `status == PAPER`:
   - Load the risk evaluation to get `position_size_units`, `stop_loss`, `take_profit`, `price`.
   - Call `self._broker.open_at_next_open(...)` with the decision's parameters.
   - Log success/failure (order rejection is not an error — it's a normal guard).
3. **No changes to `DecisionEngine`** — it remains a pure decision + persist layer.
4. **No changes to `LedgerBroker`** — it already has the exact API we need.

### 5.2 Data Flow

```
DecisionEngine.decide() returns DecideResult
   │
   ├─ status == PAPER, created == True
   │    │
   │    ▼
   │  Load RiskEvaluationRow for this (symbol, timeframe, bucket_ts)
   │  → position_size_units, stop_loss, take_profit, price
   │    │
   │    ▼
   │  LedgerBroker.open_at_next_open(
   │      symbol=symbol,
   │      timeframe=timeframe,
   │      direction=DecisionDirection(result.direction),
   │      ref_price=risk_eval.price,
   │      ts=result.bucket_ts,
   │      units=risk_eval.position_size_units,
   │      decision_id=decision_id,          ← from _latest_decision_id()
   │      stop_loss=risk_eval.stop_loss,
   │      take_profit=risk_eval.take_profit,
   │  )
   │    │
   │    ├─ Returns PaperOrderRow → order persisted, log success
   │    └─ Returns None → rejected (open position / FLAT / non-positive units), log info
   │
   ├─ status == BLOCKED, created == True → emit alert.risk_brake (existing)
   └─ always → publish decision.emitted (existing)
```

### 5.3 Transaction Boundary

The entire flow shares one `AsyncSession`:

1. `save_decision()` — inserts decision row (first-writer-wins)
2. `save_risk_evaluation()` — inserts risk evaluation linked to decision_id
3. `LedgerBroker.open_at_next_open()` — inserts order + position (via same session)
4. `refresh_exposure()` — upserts risk_state
5. `session.flush()` — all writes staged
6. Caller commits (or rolls back on exception)

If `open_at_next_open()` raises, the entire transaction rolls back — decision, risk
evaluation, and any partial order/position writes are discarded. This is safe: the
next poll will re-evaluate the same (symbol, timeframe) and produce a fresh decision.

### 5.4 Idempotency Strategy

| Layer | Mechanism | Effect |
|-------|-----------|--------|
| Decision | `ON CONFLICT (symbol, timeframe, bucket_ts) DO NOTHING` | Replay never duplicates decisions |
| Risk eval | `ON CONFLICT (symbol, timeframe, bucket_ts) DO NOTHING` | Replay never duplicates risk |
| Order | `decision_id` UNIQUE on `orders_paper` | Replay never creates duplicate order for same decision |
| Position | `uq_positions_open_symbol` (one OPEN per symbol) | Second entry for same symbol rejected at DB level |
| LedgerBroker | `open_for(sym) is not None` guard | In-memory check before DB write |

**Net effect:** At most one order + one position per PAPER decision, per symbol. Redeliveries
of `signal.emitted` are safe.

---

## 6. Exact Files to Change

| File | Change | Risk |
|------|--------|------|
| `app/workers/orchestrator_worker.py` | Inject `LedgerBroker` (optional), add PAPER fill after `decide()` | Low — additive, guarded |
| `tests/unit/test_decision_wiring.py` | New: unit tests for PAPER→LedgerBroker wiring | — |
| `tests/integration/test_decision_pipeline.py` | Extend: verify PAPER creates order+position | — |

**No changes to:**
- `app/decisions/engine.py` — remains pure decision + persist
- `app/broker/ledger.py` — API already correct
- `app/broker/adapter.py` — Protocol unchanged
- `app/models/decision.py` — statuses unchanged
- `app/models/paper_ledger.py` — schema unchanged
- `app/core/config.py` — SAFE MODE unchanged
- `app/worker_main.py` — `executor` role stays refused
- `backend/migrations/` — no new migration needed

---

## 7. SAFE MODE Analysis

### Why PAPER Cannot Become a Live Order

1. **L1 — Config:** `ALLOWED_TRADING_MODES = frozenset({"safe"})` — startup aborts on any other value.
2. **L2 — Code:** `LedgerBroker` delegates to `PaperBroker` which is purely in-memory math.
   There is no `submit_order`, `route_order`, or `place_order` method on `LedgerBroker`.
   The `BrokerAdapter` Protocol has no live-execution methods.
3. **L3 — Orchestrator guard:** `compute_decision()` returns `PAPER` only when all risk gates
   pass and `risk_enabled` is true. Even then, "PAPER" is a paper intent — the wiring calls
   `LedgerBroker.open_at_next_open()` which is paper-only.
4. **L4 — Startup:** `/health/live` exposes `mode: "safe"`. `/health/ready` refuses "ready"
   unless mode is safe. UI shows amber badge.
5. **L5 — Tests:** `test_ledger_safe_mode.py` asserts:
   - `LedgerBroker` has no execution surface (no `submit_order`, etc.)
   - `DecisionEngine` imports no `app.broker.*`
   - `executor` role is refused
   - `ALLOWED_TRADING_MODES == frozenset({"safe"})`

**The wiring does not weaken any layer.** It adds a code path from PAPER → `LedgerBroker`,
but `LedgerBroker` is structurally incapable of live execution. The test in
`test_ledger_safe_mode.py:test_decision_engine_is_not_wired_to_a_broker` checks that
`app.decisions.engine` imports no `app.broker.*` — this remains true because the wiring
is in `orchestrator_worker.py`, not in `engine.py`.

### What Would Need to Change for Live Trading

- New ADR superseding ADR-0002 with owner approval.
- New `BrokerAdapter` implementation (e.g., `OandaBroker`).
- `worker_main.py` must allow `executor` role.
- `config.py` must accept `TRADING_MODE=live`.
- All 5 SAFE MODE layers must be re-designed.
- `test_ledger_safe_mode.py` assertions must be updated first.

None of this is in scope for 13C.

---

## 8. Out of Scope

- **Executor worker role** — deferred to 13D+ (when live-execution is considered).
- **New migration** — `orders_paper.decision_id` FK and `uq_positions_open_symbol` already exist in migration 0008.
- **Separate consumer on `decisions.stream`** — deferred with Option B.
- **Equity snapshot scheduling** — `LedgerBroker.equity_snapshot()` exists but is not called by the orchestrator. Scheduling periodic snapshots is a separate concern.
- **SL/TP exit evaluation** — `LedgerBroker.evaluate_exit()` is called on bar close, not on decision. The existing `evaluate_exit` path in the backtest driver is the model; a production equivalent needs bar-close event handling (separate concern).
- **Live order routing** — structurally impossible in SAFE MODE.

---

## 9. Tests Required

### 9.1 Unit Tests (`tests/unit/test_decision_wiring.py`)

| Test | What it asserts |
|------|-----------------|
| `test_paper_decision_creates_order_and_position` | When `DecideResult` has `status=PAPER` and `created=True`, `LedgerBroker.open_at_next_open()` is called and returns a `PaperOrderRow`. Order has correct `decision_id`, symbol, direction, units, stop_loss, take_profit. Position is OPEN. |
| `test_analysis_decision_does_not_create_order` | When `status=ANALYSIS`, no call to `open_at_next_open()`. Orders table remains empty. |
| `test_blocked_decision_does_not_create_order` | When `status=BLOCKED`, no call to `open_at_next_open()`. Orders table remains empty. |
| `test_duplicate_delivery_does_not_create_second_order` | Same (symbol, timeframe, bucket_ts) processed twice → `created=False` on second pass → no second order. |
| `test_order_rejection_does_not_crash_pipeline` | When `open_at_next_open()` returns `None` (e.g., position already open), orchestrator continues normally, publishes `decision.emitted`. |
| `test_broker_failure_rolls_back_transaction` | When `open_at_next_open()` raises, the entire session rolls back — decision, risk eval, and any partial writes are discarded. |
| `test_safe_mode_no_live_execution_path` | Structural: `OrchestratorWorker` imports no `app.execution.*` or `app.orders.*`. `LedgerBroker` has no `submit_order`/`route_order` method. |

### 9.2 Integration Tests (`tests/integration/test_decision_pipeline.py` — extend)

| Test | What it asserts |
|------|-----------------|
| `test_paper_decision_persists_order_and_position` | Full pipeline: seed signals → run engine → verify `orders_paper` has one FILLED row, `positions` has one OPEN row, linked by `decision_id`. |
| `test_paper_order_idempotent_on_replay` | Run engine twice with same inputs → one order, one position (first-writer-wins + unique decision_id). |
| `test_paper_order_rejected_when_position_open` | Seed signals → run engine (creates position) → run again with new bucket → second order rejected (one-OPEN-per-symbol). |

---

## 10. Acceptance Criteria

1. A PAPER decision produces exactly one `orders_paper` row (status=FILLED) and one `positions` row (status=OPEN).
2. An ANALYSIS or BLOCKED decision produces zero orders and zero positions.
3. Duplicate delivery of the same `signal.emitted` does not create duplicate orders.
4. If `LedgerBroker.open_at_next_open()` raises, the decision is not persisted (transaction rollback).
5. If `LedgerBroker.open_at_next_open()` returns `None` (rejected), the pipeline continues normally.
6. All existing tests pass unchanged.
7. New unit tests cover: PAPER creates order, ANALYSIS/BLOCKED do not, duplicate idempotency, rejection handling, failure rollback.
8. New integration test verifies end-to-end: signals → decision → order + position in PostgreSQL.
9. `test_ledger_safe_mode.py` assertions still pass (no new live-execution surface).
10. `ruff check`, `ruff format --check`, `mypy`, `pytest tests/unit` (≥90% coverage), `pytest tests/integration` all green.

---

## 11. Recommended Implementation Sequence

1. **Write unit tests first** (`tests/unit/test_decision_wiring.py`) — define expected behavior.
2. **Inject `LedgerBroker`** into `OrchestratorWorker.__init__()` (optional parameter, default `None`).
3. **Add PAPER fill logic** in `process_pair()` after `engine.decide()` returns.
4. **Write integration test** extending `test_decision_pipeline.py`.
5. **Run gates:** `ruff check`, `ruff format --check`, `mypy`, `pytest tests/unit`, `pytest tests/integration`.
6. **Update `test_ledger_safe_mode.py`** if needed (add structural assertion that wiring is in `orchestrator_worker`, not `engine`).
7. **Commit** as a single atomic change.

---

## 12. Report

| Question | Answer |
|----------|--------|
| Where does the wiring go? | `OrchestratorWorker.process_pair()` in `app/workers/orchestrator_worker.py` — after `DecisionEngine.decide()` returns, before `publish_decision()`. |
| Inline or separate consumer? | **Inline** (Option A). No new worker role, no new consumer group, atomic transaction. |
| What data flows? | `DecideResult` (status, direction, symbol, timeframe, bucket_ts) + `RiskEvaluationRow` (units, stop_loss, take_profit, price) → `LedgerBroker.open_at_next_open(decision_id=...)`. |
| How is idempotency guaranteed? | First-writer-wins on decision + unique `decision_id` on orders + one-OPEN-per-symbol constraint. Redeliveries are safe. |
| What is the transaction boundary? | Decision save + risk eval save + order/position save share one `AsyncSession`. Failure rolls back everything. |
| Does this weaken SAFE MODE? | **No.** All 5 layers remain intact. `LedgerBroker` delegates to `PaperBroker` (in-memory math only). No live-execution surface is added. The structural test in `test_ledger_safe_mode.py` passes because wiring is in `orchestrator_worker.py`, not `engine.py`. |
| What files change? | `app/workers/orchestrator_worker.py` (production), `tests/unit/test_decision_wiring.py` (new), `tests/integration/test_decision_pipeline.py` (extend). |
| What is out of scope? | Executor worker role, new migration, separate consumer, equity snapshot scheduling, SL/TP exit evaluation, live order routing. |
| What tests are required? | 7 unit tests + 3 integration tests (see §9). |
| When is this done? | All 10 acceptance criteria pass (see §10). |

**STOP.** This audit is complete. No code changes were made beyond creating this document.
