# Phase 6 Implementation Proposal — Backtesting Engine

**Status:** PROPOSAL — for review. No code changed, nothing committed or pushed.
**Date:** 2026-08-27
**Base:** `main` at `1875fec` (clean tree)

---

## 0. Executive Summary

Phase 6 builds an offline **backtesting engine** that replays the exact live
decision pipeline (agents → orchestrator/decision engine → risk → fills) over
historical candles and produces a reproducible P&L report. The guiding invariant
(plan §14 Phase 6, line 659) is: *identical signals for identical bars* — the
backtest must run the *same code path* that the live SAFE MODE system uses, so
results are trustworthy.

The single most important thing the inspection surfaced: **the plan placed the
Paper Executor in Phase 5, but the Phase 5 PR deferred it, explicitly to Phase 6**
(`decision_repository.py:233-234`, `worker_main.py:8`). A backtest that reports
P&L *requires* a fills/equity/cost model. This proposal therefore folds the
deferred **paper executor's fill + cost model** into Phase 6 as a shared,
deterministic component reused by both the backtester and (later) live paper
trading. That resolves the deferral cleanly and matches the plan's Phase 5 exit
criterion that was not yet met. **Backend-only** — no frontend changes (plan
places the Backtests page in Phase 9).

---

## 1. Goals & Non-Goals

### Goals
1. Deterministic, offline replay of the **exact** agent/orchestrator/risk code path
   over historical candle data.
2. A shared **paper broker** (fills, spread + slippage costs, positions, equity,
   realized pnl) that is deterministic and cost-parity with future live paper ops.
3. Reproducible reporting: net/gross PnL, win-rate, profit factor, Sharpe/Sortino,
   max drawdown, exposure, per-regime breakdown; equity + drawdown curves persisted.
4. Persistence of every run in `backtest_runs` so runs are comparable and auditable
   (config, data range, seed, code versions, metrics).
5. A `CLI backtest run` command and a compare view API surface (v1: minimal).

### Non-Goals (this phase)
- **No live/trading implications.** SAFE MODE is preserved at L1–L5: backtests
  write only to `backtest_runs`/analysis tables — never orders/positions that a
  live executor could act on. The `executor` worker role stays refused
  (`worker_main.py:8`).
- No WebSocket broadcast of backtest progress.
- No walk-forward *frontend*; walk-forward *scaffolding* only (per plan line 663).
- No changes to `decisions/engine.py`, `fusion.py`, `risk.py`, `agents/*`, or
  the live orchestrator behavior. The engine is *reused*, not rewritten.
- No frontend work (Phase 9).

---

## 2. Current-State Audit (what exists, what's missing)

Confirmed by inspection on `1875fec`:

**Reusable (pure / deterministic):**
- `agents/base.py` — `AnalysisContext`, `AgentSignal`, `BaseAgent.analyze` (sync, pure).
- `decisions/engine.py:compute_decision` — pure, deterministic transformation
  (line 125). Its DB brother `DecisionEngine.decide` (line 309) is the live path.
- `decisions/fusion.py` (`fuse`, `apply_context`), `decisions/risk.py`
  (`assess`, `compute_sizing`) — pure.
- `data/providers/synthetic.py` — deterministic, order-independent OHLCV
  generation (bytes-identical per `(symbol, tf, bucket)`). **Ideal backtest data
  source** and supports arbitrary ranges without genesis replay.
- `data/repository.py` (`load_candles`, `upsert_candles`, `get_or_create_instrument`).
- `cli.py` (Typer) — home for the new `backtest` command group.

**Missing (must be built):**
- **No broker/executor/backtest module** (`PaperBroker` mentioned only in docs).
- No fills, positions, equity curve, or realized pnl logic.
- No `backtest_runs` table or migration (next is `0006_phase6_backtest.py`).
- No CLI `backtest` command.
- No `tests/backtest/` suite.

**Friction points the backtester must handle:**
1. `DecisionEngine.decide` is DB-coupled and **time/`now`-coupled**:
   - `_gate_state` reads exposure from `load_active_paper_snapshot` which derives
     *from persisted decisions+risk evals* (not from a real ledger). In backtest,
     the *broker's* open positions must be the source of exposure — not stale DB
     decisions.
   - `_parent_context` reads the *largest configured timeframe's latest decision*
     from DB. In backtest this is fine if we persist decisions per bar (as in live),
     but we must process timeframes in rank order (D1→H4→H1→M15…) so the parent
     decision exists before the child is evaluated.
   - `_resolve_atr_price` uses the latest stored candle ≤ bucket and the technical
     signal's ATR. Backtest must feed candles up to-but-not-including the current
     bar only (see **no look-ahead**, §7).
2. **Exposure source of truth must switch** to the simulated broker position set
   during a backtest run (see §5 design decision).
3. **News/calendar/LLM are not replayed.** Phase 4 fundamental/sentiment agents
   consume current-window news; backtesting historical fundamental/sentiment
   requires either (a) persisted historical news and re-running those agents, or
   (b) replaying *persisted historical signals*. See §4 scope decision (needs
   approval).

---

## 3. Architecture & Data Flow

```
historical candles (synthetic OR stored PG)
        │
        ▼
[ ReplayDriver ]  ── walks timeframes in rank order (D1→…→M15), bar-by-bar
        │
        ▼
[ AgentRunner ]   ── builds AnalysisContext(candles window ≤ current bar),
        │              calls the *same* BaseAgent.analyze objects the live
        │              pipeline uses; emits AgentSignal
        ▼
[ OrchestratorSim ] ── replicates live poll semantics: gather per-agent signals
        │               at bucket, build DecisionInputs, call compute_decision
        │
        ▼
[ RiskGate ]       ── assess(…, gate=from BROKER positions) → status (PAPER/…)
        │
        ▼  on PAPER intent at bar close
[ PaperBroker ]    ── fills at next bar's open (no look-ahead) with spread+
                      slippage; manages positions/equity/realized pnl; clears
                      SL/TP intrabar (disabled in v1 — see §4), else fills
        │
        ▼
[ RunReport ] ── aggregates metrics; persists backtest_runs + equity/drawdown
```

Key properties:
- **Same code path:** `BaseAgent.analyze` + `compute_decision` + `assess` are
  invoked directly (not duplicated). Only the *data acquisition* layer differs:
  backtest serves historical `AnalysisContext`/`DecisionInputs` instead of DB live
  rows. This is the "same signal path" invariant guard (plan line 663 / §13).
- **Bar-close only:** a decision for bucket `t` is evaluated using only candles
  with `ts <= t`; the fill price is at the **open of the next bucket**. No future
  data — asserted by tests (§7).

---

## 4. Scope Decision (REQUIRES APPROVAL)

The plan is ambiguous on two coupled points. I need decisions before implementation:

### D-A. Include the deferred Paper Executor fill/cost model in Phase 6?
- **Option 1 (Recommended): Yes.** Phase 5 deferred it; a P&L backtest needs it.
  Build `app/broker/` (PaperBroker, fills, positions, costs) as a deterministic
  shared component. This *satisfies* the unmet Phase 5 exit item ("Paper executor
  worker v1" / autonomous paper loop) in a phase-appropriate way — the live
  `executor` worker role remains refused, but the *simulation core* exists and is
  unit-tested.
- **Option 2: No — decision-replay only (no fills/PnL).** Backtest reports only
  decisions/signals, not trading PnL. Weaker report, but smaller scope; fills/PnL
  moves to a later phase. I advise against: the plan explicitly requires
  net/gross PnL, win-rate, Sharpe, etc.

### D-B. How to handle historical fundamental/sentiment inputs?
- **Option 1 (Recommended): Replay persisted historical signals.** If Phase 4/5
  already persisted `agent_signals` for the range, the backtester reads those rows
  and feeds them into the orchestrator exactly as live would. This is
  computationally cheap and the *most faithful* replay of what actually happened.
- **Option 2: Re-run news/calendar + LLM agents.** Only possible if historical
  news/calendar rows exist for the range; re-running the LLM is non-deterministic
  (budget, provider) unless fully deterministic fallback is used, and defeats
  determinism. I advise against for ranges without persisted content.
- **Option 3 (v1 pragmatism, needs approval on exit criteria):** Default to replay
  of persisted signals; if absent, run technical+regime agents from candles and
  *skip* fundamental/sentiment (documented coverage), rather than silently
  fabricating votes. **Question:** is degraded coverage acceptable for v1 exit
  ("reproduce a prior week bit-for-bit")? This interacts directly with
  `orch_min_agent_coverage` and the fail-closed behavior.

### D-C. Intrabar SL/TP simulation
- **Option 1 (Recommended for v1):** No intrabar fills — positions clear only on a
  **next-bar signal to close** or an **intrabar touch of SL/TP evaluated at
  subsequent-bar OHLC** (conservative, simple, deterministic, no look-ahead).
- **Option 2:** High/low intrabar fill touch simulation (more realistic, more
  code, more edge cases). Defer to a later phase.

---

## 5. Key Design Decisions (recommended)

| # | Decision | Rationale | Reuses |
|---|---|---|---|
| 1 | **Broker = exposure source of truth in backtest.** `RiskGate` gets `GateState` computed from the simulated broker's *open positions* (notorm claimed via `load_active_paper_snapshot`), exactly like `_gate_state` intends but from a real ledger. | Live exposure is decision-derived today only because no ledger exists; the broker IS the ledger. This is the honest abstraction `decision_repository.py:233-234` anticipates. | `risk.GateState` unchanged |
| 2 | **Shared `app/broker/paper.py` PaperBroker** (pure/deterministic) + `app/broker/costs.py` (spread/slippage model) + `app/broker/positions.py`. | Single fill/cost model reused by backtester now and live paper later — guarantees "cost parity with paper broker" (plan line 660). | `directions.Direction`, `risk.compute_sizing` (SL/TP/units) |
| 3 | **`app/backtest/` package**: `driver.py` (replay), `agent_runner.py`, `orchestrator_sim.py`, `report.py`, `repository.py` (persist), `models.py`. | Mirrors live module boundaries; keeps backtest isolated but reusing `agents/`, `decisions/`, `data/`. | |
| 4 | **Replay driver is determinism-first**: ordered bucket iteration, fixed `seed` for any randomness, `freezegun`-style fixed `now` per bucket, strict UTC (`tzinfo=UTC`, never host-local). | Lesson from the Phase 4 CI failure: host-TZ-dependent logic is a bug. Backtest must be byte-identical regardless of CI runner TZ. | `align_to_bucket`, `iterate_buckets` |
| 5 | **No look-ahead enforced structurally**: candles window `[warmup, bucket]` inclusive of current bar only; fills at next open. | Failure mode the plan calls out (look-ahead/data-snooping, line 714). | |
| 6 | **Config snapshotted** into `backtest_runs.config` (JSONB) + `code_versions` (agent `version` strings + backtest module versions) for audit/reproducibility. | Plan §9 line 498. | `decisions.hashing.inputs_hash` pattern |
| 7 | **Alembic migration `0006`**: `backtest_runs`, `backtest_equity` (equity curve), `backtest_trades` (fills), `backtest_drawdown` (or fold equity+dd into one curve table). | Matches plan table sketch (line 498). | migration chain |

---

## 6. Project Structure Changes (backend only)

New directories/modules:

```
backend/app/
  broker/
    __init__.py
    costs.py        # spread + slippage model (deterministic, seeded)
    positions.py    # Position/PositionSet, unrealized/realized pnl
    paper.py        # PaperBroker: fill on next open, SL/TP eval, equity
  backtest/
    __init__.py
    models.py       # BacktestConfig, RunMetrics dataclasses/pydantic
    agent_runner.py # build AnalysisContext per bucket; call real agents
    orchestrator_sim.py  # assemble DecisionInputs + call compute_decision
    driver.py       # ordered replay loop (timeframes rank-ordered)
    report.py       # metrics aggregation (sharpe, profit factor, dd, ...)
    repository.py   # persist backtest_runs/equity/trades
  models/backtest.py        # SQLAlchemy rows
  migrations/versions/0006_phase6_backtest.py

backend/tests/
  unit/  test_broker_costs.py, test_broker_positions.py,
         test_broker_paper.py, test_backtest_driver_determinism.py,
         test_backtest_no_lookahead.py, test_backtest_report.py
  integration/
         test_backtest_replay.py (decision parity with live path)
         test_backtest_persistence.py
  backtest/  (plan naming: tests/backtest/ seed helpers, scenario fixtures)

backend/scripts/backtest/
  run_example.sh          # example: reproduce last week from synthetic
  seed_historical.py      # seed a deterministic historical set

cli: `app/cli.py` add command group `backtest`:
  python -m app.cli backtest run --range STARt..END --pairs EURUSD,GBPUSD \
      --timeframes M15,H1,H4 --config config.json --seed 42
  python -m app.cli backtest list / show <run_id>
```

No changes to: `decisions/*` (read-only reuse), `agents/*`, `workers/orchestrator_*`
(live loop untouched), `config.Settings` (add any backtest-only settings as
optional fields, SAFE defaults), `.github/workflows/backend-ci.yml` (unit job
already runs `tests/unit`; ensure new suites honored by markers).

---

## 7. Determinism, Look-Ahead, and Cost Model

**Determinism (the hard requirement):**
- All sources of entropy are pinned: `seed` recorded and used for slippage noise;
  no `datetime.now()` — each bucket's `now` is derived from its timestamp; `UTC`
  everywhere (no host-local `astimezone()` — cite Phase 4 lesson).
- Candles come from `synthetic.generate_candle` (already deterministic) or read
  from PG; iteration uses `iterate_buckets`/`align_to_bucket`.
- CI must produce **byte-identical** report JSON for identical `config + range +
  data + seed` — enforced by a test that runs the same input twice under
  `TZ=UTC` and `TZ=America/New_York`.

**No look-ahead:**
- Decision for bucket `t`: agents receive candles with `ts <= t` (warmup window
  sized per indicators, e.g. ≥ 60 bars for ATR/ADX).
- Fill: at the **next bucket's open** using *that* bucket's candle open.
- SL/TP: evaluated against *subsequent* buckets' high/low only after entry;
  a position opened at `t+1` cannot be closed at `t+1` intrabar (conservative).

**Cost model (`broker/costs.py`):**
- Per-pair spread in pips (`PAIR_SPECS` pip size); apply spread on entry and exit.
- Slippage: deterministic function (seeded) of (direction, liquidity proxy, notional).
- Flat per-trade commission placeholder (0 for forex P&L parity; configurable).
- All baked into `PaperBroker`, so backtest and live paper share identical math.

---

## 8. Reporting & Metrics

`report.py` computes from the simulated equity/trades:
- Net & gross PnL, round-trip trade count, win-rate, avg win/loss.
- Profit factor; Sharpe & Sortino (annualized from bar frequency); max drawdown
  (fraction + duration).
- Average exposure %; per-regime breakdown (regime label per decision from signal
  features) of wins/losses.
- Equity curve + drawdown curve persisted; a JSON summary in `backtest_runs.metrics`.

Prometheus: add (optional) `backtest_runs_total{status}`, `backtest_duration_ms`
counters/histograms — low priority; backtest is offline CLI.

---

## 9. Database & API Surface

**Migration `0006`:**

| Table | Key columns | Purpose |
|---|---|---|
| `backtest_runs` | id UUID PK, config JSONB, data_range, seed, code_versions JSONB, metrics JSONB, status, started_at, finished_at | reproducible run header + result summary |
| `backtest_trades` | run_id FK, symbol, side, entry_ts/price, exit_ts/price, units, gross/net_pnl, costs JSONB | every simulated fill for audit/replay |
| `backtest_equity` | run_id FK, ts, equity, drawdown_pct | equality/drawdown curve |

**API (minimal, v1):** `GET /api/v1/backtests` (list+summary), `GET
/api/v1/backtests/{id}` (detail incl. metrics), `GET
/api/v1/backtests/{id}/equity`, `GET /api/v1/backtests/{id}/trades`,
`GET /api/v1/backtests/{id}/compare?against=` (side-by-side metrics). Read-only,
auth required (viewer+). Full CRUD/launch UI deferred to Phase 8/9.

---

## 10. Acceptance Criteria (tests + gates)

**Behavioral exit criteria (plan line 664):**
1. **Bit-for-bit reproduction:** replaying a prior synthetic week reproduces the
   same decisions/signals as the live path for that same data (integration test).
2. **Determinism test:** identical `config+range+seed` ⇒ byte-identical report,
   TZ-independent (run under `TZ=UTC` and `TZ=America/New_York`).
3. **No-look-ahead test:** a regression test proves no future bar is read (inject
   a sentinel future value; assert unused).
4. **Cost sensitivity:** increasing spread reduces net pnl monotonically; zero-cost
   equals gross pnl (unit tests).
5. **Scenario suites:** pure trend → long bias; flat/range → mostly no-trade;
   seeded identical → identical output.

**Gates (must all pass, matching Phase 5 style):**
- `ruff format --check .`, `ruff check .`, `mypy app` — clean.
- `pytest tests/unit` coverage gate stays green (new modules included in gate per
  plan §13: coverage ≥85% on `backtest/`, `broker/`).
- `pytest tests/integration` green (real PG + Redis).
- **SAFE MODE regression (marked critical):** (a) no live-broker module importable /
  config rejects non-safe mode; (b) a full backtest run emits **zero** non-paper
  orders — DB contains only `backtest_*` rows, no `orders_paper`/`positions`
  created by the backtester.
- `git diff --check` clean; no `.env` tracked; SAFE MODE/banner intact.

---

## 11. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Exposure double-source (DB decisions vs broker) causing inconsistent gates | Wrong risk results | Broker is the single source during replay; `_gate_state`-style math reused, fed from `PositionSet`. Test asserts consistency. |
| Look-ahead/data-snooping bug | Invalid results | Structural windowing + dedicated no-look-ahead test + UTC monotonic checks. |
| Non-determinism reintroduced (TZ, now()) | Unreproducible runs | Determinism test runs both TZ envs; `now` derived from bucket; seed pinned. |
| Fundamental/sentiment replay absence → fail-closed to no-trade | Weak reports if coverage < min | D-B decision + clear reporting of coverage; v1 may accept technical+regime-only runs. |
| Missing THEN-unbuilt paper broker blocks PnL | Scope surprise | D-A decision — recommended fold broker into Phase 6. |
| Migration/DDL risk on live DB | Not applicable | Backtest tables are additive, non-mutating to existing data. |
| Backtest module drift from live decision code | False confidence | The same `compute_decision`/`assess`/agent objects injected; a parity test guards drift. |

---

## 12. Implementation Sequencing (suggested, within phase)

1. Migration `0006` tables + models.
2. `broker/` package: costs, positions, PaperBroker (pure) + unit tests.
3. `backtest/` scaffolding: replay driver loops over timeframes/buckets calling
   real agents + `compute_decision` + `assess` (exposure from broker).
4. Fundamental/sentiment input strategy per **D-B**; wire into orchestrator_sim.
5. `report.py` metrics + persistence + CLI `backtest run/list/show`.
6. API read endpoints.
7. Determinism / no-look-ahead / parity / SAFE-MODE tests; coverage gates.
8. Full gates + docs (`docs/adr/0006-backtesting.md` optional); review.

---

## 13. Decisions Required From You

1. **D-A** — Build the shared Paper Broker (fills/cost model) in Phase 6? (Recommended: Yes.)
2. **D-B** — Historical fundamental/sentiment strategy: replay persisted signals
   (rec.) vs re-run agents vs technical+regime-only with documented degradation
   (and is degraded coverage acceptable for the "reproduce prior week" exit test?).
3. **D-C** — Intrabar SL/TP: next-bar conservative (rec.) vs high/low touch sim.
4. **D-open-1** — For the bit-for-bit exit test, is a **synthetic** historical range
   acceptable as the "prior week" (since no live paper history exists yet)?
5. **D-open-2** — Reporting frequency/annualization basis for Sharpe/Sortino
   (default: per configured bar frequency; confirm OK).
6. **D-open-3** — Confirm Phase 6 remains **backend-only** (Backtests page = Phase 9).

## 14. Open items to confirm with you before writing code
- Confirm CLI surface/flag naming (`backtest run --range START..END` etc.).
- Confirm we publish `backtest_runs` rows to `decisions.stream`/Redis? (Recommend:
  no for v1 — offline CLI writes PG directly; avoids leaking backtest into live bus.)

---

*This document is a proposal only. Nothing has been modified, committed, or pushed.
Awaiting your decisions (esp. D-A / D-B / D-C) before implementation begins.*
