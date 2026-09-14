# Phase 13 — Phase B Audit & Plan: Read-Only Portfolio Observability

**Type:** Audit + implementation plan (no code in this milestone).
**Constraint:** This audit is **read-only**. It creates no project files besides this document, installs nothing, changes no schema, and runs no migrations. It ends with a STOP — no commit, tag, or push is performed.

---

## 1. Baseline

- `v0.1.0` release (tag + commit `d28af88`) with Phase A layered on top: commit `84bda82` **"feat: add paper-trading ledger foundation"** (13 files, +1773/−3). Verified before commit: migration `0008` up/down on a scratch DB, `alembic check` shows no drift on the new tables, `ruff`/`mypy` clean, unit 481 @ 92.87% (gate ≥ 90%), integration 105 passed, SAFE MODE tests green.
- Working tree is clean; `main` is **ahead of `origin/main` by 1** — nothing has been pushed.
- Runtime state: **SAFE MODE** pinned (`TRADING_MODE=safe`, `executor` worker refuses to start). PAPER decisions are **not** wired to `LedgerBroker` yet; the ledger tables are empty. There is no live trading and no way to create one in this milestone.

### Provenance of the ledger (what Phase B reads)

Migration `0008` plus ORM in `backend/app/models/paper_ledger.py` define three durable tables:

| Table | Purpose | Key columns |
|---|---|---|
| `orders_paper` | Paper orders (fill policy `NEXT_OPEN`). | `decision_id` (FK `decisions.id`, **SET NULL**, unique), `symbol(12)`, `timeframe(4)`, `side`, `order_type`, `status` (PENDING/FILLED/CANCELLED/REJECTED), `units` (20,6), `requested_price`/`filled_price`/`stop_loss`/`take_profit` (18,8), `costs` (20,6), `seed`, `filled_at`. Indexes: `(status, created_at)`, `(symbol)`. |
| `positions` | Simulated positions; **at most one OPEN per symbol** (`uq_positions_open_symbol`). | `order_id` (FK `orders_paper.id`, RESTRICT), `symbol`, `timeframe`, `side`, `units` (20,6), `entry_price` (18,8), `entry_ts`, `stop_loss`/`take_profit`, `costs`, `status` (OPEN/CLOSED), `exit_price`, `exit_ts`, `exit_reason(16)`, `gross_pnl`/`net_pnl` (20,6, null until closed). |
| `account_snapshots` | Point-in-time `BrokerState`. | `ts`, `cash`, `equity`, `open_pnl`, `realized_pnl`, `peak_equity`, `margin_used` (20,6), `drawdown_pct` (12,8), `extra` (JSONB). Index: `(ts)`. |

All extend `UUIDPrimaryKeyMixin` + `TimestampMixin`.

---

## 2. Audit findings (the 20 points)

### 2.1 Ledger schema/ORM — fit, no changes needed

The Phase A tables are sufficient for every read this milestone needs. Positions carry full open details; closed positions carry round-trip `gross_pnl`/`net_pnl`; snapshots carry cash/equity/drawdown/peak/margin. **Zero DB or migration work.** Indexes already support the two sort modes we need (`positions.entry_ts`, `orders_paper.created_at`, `account_snapshots.ts`, and status filters). No new enums or constraints.

### 2.2 API architecture — mirror the read-only routers

FastAPI routers live in `backend/app/api/v1/*.py`, each `APIRouter(prefix="/…", tags=[…])`, mounted in `backend/app/main.py` under `API_V1_PREFIX` (`/api/v1`). Typed Pydantic response models with `from_attributes`; routers map `Decimal`/raw rows to floats in thin `_out` helpers (see `backtests.py` `_run_out`, `market.py` `_candle_out`).

**Precedent to copy byte-for-byte in style:** `app/api/v1/backtests.py`
- read-only: `GET /backtests` (list, `limit` query), `GET /backtests/{id}`, `/trades`, `/equity`;
- dependency signature `session: DBSession, current: CurrentUser` (any authenticated user = **viewer+**);
- raises `NotFoundError` for a missing resource; returns empty arrays for empty history;
- `float(row.equity)` style conversion for `Numeric` → JSON numbers.

### 2.3 Frontend architecture — one new page, zero new dependencies

Next.js App Router (Next 15 / React 19 / Tailwind v4). Pages are self-contained `"use client"` components: `authFetch` from `src/lib/auth.ts`, session gating via `getToken()` → redirect `/login`, mode badge + **observe-only** pill, header with inline nav links, tables, and **hand-rolled SVG charts** — there is **no chart library**. `lib/backtests.ts` already exports battle-tested pure helpers: `buildPath`, `buildEquityPath`, `buildDrawdownPath`, `normalizeForComparison`, and formatters `fmtCurrency`/`fmtPct`/`fmtNumber`/`fmtDateTime`. Tests: `vitest` against the lib modules (`backtests.test.ts` etc., 4 test files today).

**The Backtests page is the exact UI template** for a Portfolio page (`simplest vertical slice`: auth gate, mode badge, observe-only pill, summary cards, two tables, one or two SVG chart blocks, graceful empty states).

### 2.4 Realtime architecture (WS) — defer, REST polling only

Phase 8 WS (`app/api/v1/realtime.py` + `app/ws/hub.py`) already exists: one-time ticket + `/ws/stream`, topics `alerts` / `signals` / `decisions`, RBAC topic isolation, non-destructive XREAD. Crucially, the `fills` stream is **RESERVED and not subscribable** — the read path for order/position fills is explicitly earmarked for a later phase (the executor). A portfolio WS topic would require a new durable Redis stream + a producer + ticket/topic plumbing **before it can push anything**, and with the executor unwired there is nothing to push anyway.

**Decision: defer all realtime/Redis work.** `GET` polling on page load (exactly how the Backtests page works) is the phase-appropriate pattern. WS for portfolio belongs with the `fills` topic + executor phase.

### 2.5 Reuse of decision/order/position concepts

- **Order/position/snapshot concepts:** reuse the Phase A models directly — they are already the single source of truth (`app/models/paper_ledger.py`).
- **`decision_id` correlation:** `PaperOrderRow.decision_id` (SET NULL, unique constraint) links each order to the decision that produced it (Phase C wiring). The read view joins `positions → orders_paper → decisions` to expose `decision_id` (nullable when SET NULL fired). Endpoint fields mirror `DecisionOut.symbol/timeframe` conventions so the frontend can cross-link.
- **Existing exposure concept — do NOT reuse for the summary:** `decision_repository.load_active_paper_snapshot()` derives exposure from **still-valid PAPER decisions** (units×price), an explicitly temporary pre-ledger mechanism. The ledger snapshots (`cash`/`equity`/`open_pnl`) are the correct truth for portfolio observability. Reconciling the two is out of scope; the milestone reads ledger data only.

### 2.6 Read-only endpoints (proposed — the Phase B vertical slice)

All under `backend/app/api/v1/portfolio.py`, prefix `/portfolio`, tags `["portfolio"]`. All **GET, viewer+**, read-only. Name follows the existing `backtests`/`market`/`decisions` conventions; there is currently no `portfolio.py` (confirmed — no collision).

1. **`GET /api/v1/portfolio/summary`** → `PortfolioSummaryOut`
   Latest `account_snapshots.ts` row + live counts. Empty ledger → zeros with `as_of=None` (friendlier empty state than 404; SAFE MODE shows "no paper activity yet").
2. **`GET /api/v1/portfolio/positions`** → `list[PortfolioPositionOut]`
   `status` filter (`OPEN`/`CLOSED`), `limit` int, ordered by `entry_ts` desc. Includes `decision_id` (via order join, nullable).
3. **`GET /api/v1/portfolio/orders`** → `list[PortfolioOrderOut]`
   `status` filter, `limit` int, ordered by `created_at` desc. Includes `decision_id` (nullable).
4. **`GET /api/v1/portfolio/equity`** → `list[PortfolioEquityPointOut]`
   Snapshot history ordered by `ts` **asc** (correct for the equity chart; the backtest equity endpoint follows the same chronological convention). `limit` int.

No pagination cursors — `limit` clamp exactly like `market.py` (`Annotated[int, Query(ge=1, le=1000)] = 500`) and `backtests.py` (default 20–50). No `PUT`/`POST`/`DELETE` anywhere; no executor, broker, or write imports reach this router.

### 2.7 Exact response schemas

`backend/app/schemas/portfolio.py` (from_attributes, `Decimal`→`float` in mapper, mirrors `schemas/backtests.py` structure):

- `PortfolioSummaryOut`: `as_of: datetime | None`, `cash: float`, `equity: float`, `open_pnl: float`, `realized_pnl: float`, `peak_equity: float`, `drawdown_pct: float`, `margin_used: float`, `open_positions: int`, `open_orders: int`.
- `PortfolioPositionOut`: `id: str`, `order_id: str`, `symbol: str`, `timeframe: str`, `side: str` (LONG/SHORT), `units: float`, `entry_price: float`, `entry_ts: datetime`, `stop_loss: float | None`, `take_profit: float | None`, `status: str` (OPEN/CLOSED), `exit_price: float | None`, `exit_ts: datetime | None`, `exit_reason: str | None`, `gross_pnl: float | None`, `net_pnl: float | None`, `decision_id: str | None`.
- `PortfolioOrderOut`: `id: str`, `decision_id: str | None`, `symbol`, `timeframe`, `side`, `order_type` (NEXT_OPEN), `status` (PENDING/FILLED/CANCELLED/REJECTED), `units: float`, `requested_price: float | None`, `filled_price: float | None`, `costs: float`, `stop_loss: float | None`, `take_profit: float | None`, `filled_at: datetime | None`, `created_at: datetime`.
- `PortfolioEquityPointOut`: `ts: datetime`, `cash: float`, `equity: float`, `open_pnl: float`, `realized_pnl: float`, `peak_equity: float`, `drawdown_pct: float`, `margin_used: float`.

Mapping style copies `backtests.py` (`float(row.equity)`, etc.). Status strings validated into the enum literal set in the mapper (same cast pattern already used for backtest status).

### 2.8 Auth/RBAC per endpoint

- All four: **any authenticated user** (`current: CurrentUser`, viewer+). This matches the read-only precedent set by `backtests.py` and `decisions.py` (readers are viewer-accessible after login).
- Anonymous → `401` (auto by `get_current_user`); no admin-only gate (risk params/state stay admin-only in `risk.py`; portfolio observability is not that sensitive).
- Verified in integration via the `test_permissions_matrix.py` pattern (`register_and_login` + direct role UPDATE, asserts `401`/`403`/`200`; the matrix file itself is untouched — new cases live in the new portfolio test file).

### 2.9 Database changes — **none**

No migrations, no new tables/enums/columns, no `alembic revision`. The audit also treats "DB changes to help frontend" as forbidden; everything needed already exists in `0008`.
*Caveat:* with no executor wiring (Phase C), the tables stay empty in SAFE MODE, so the UI must render a truthful empty state. That is expected and desirable, not a defect.

### 2.10 Frontend: page(s), nav, charting

- **One new page:** `frontend/src/app/portfolio/page.tsx` (mirror `backtests/page.tsx`): auth gate → login, mode badge from `fetchServerMode()`/`isSafeMode`, **observe-only** pill (title: "reads paper ledger state; never initiates trades"), summary card row, Positions table, Orders table, equity + drawdown SVG charts.
- **Nav:** pages keep headers inline; add a `/portfolio` anchor to the three existing page headers (`backtests`, `signals`, `alerts`) and links back from the portfolio header. Three tiny pure-anchor edits; no shared nav component exists to edit.
- **Charts:** reuse the tested SVG builders — import `buildEquityPath`/`buildDrawdownPath`/`buildPath` and the formatters from `src/lib/backtests.ts` (they are exported, pure, unit-tested). **No new dependency** (no chart lib in `package.json`, confirmed). If reviewers prefer decoupling later, extracting to a shared `lib/svg.ts`/`lib/format.ts` is a mechanical follow-up, not part of this slice.
- **Client lib:** `frontend/src/lib/portfolio.ts` (types, defensively-typed normalizers, `fetchPortfolioSummary`/`fetchPositions`/`fetchOrders`/`fetchEquity` via `authFetch`, formatters) + `frontend/src/lib/portfolio.test.ts` (vitest, styled after `backtests.test.ts`).

### 2.11 Redis/WS deferral — confirmed deferral

No new Redis streams, topics, tickets, or WS producer. The `fills` stream is reserved for the executor phase; portfolio push belongs there. Phase B = page-load REST polling.

### 2.12 SAFE MODE implications

Strictly read-only: `SELECT` only across the three ledger tables; the router imports no executor/broker write path; `LedgerBroker`/`PostgresLedgerStore` are **not** instantiated by Phase B code (reads go through a small reader layer). Integration tests assert GETs never change row counts or snapshot contents. SAFE MODE banner/mode logic untouched — a read-only milestone is compatible by construction.

### 2.13 Exact files

**Create (backend):**
- `backend/app/schemas/portfolio.py`
- `backend/app/data/portfolio_reader.py` (small read-only layer: AsyncSession-based selects + aggregation — follows the `app/data/decision_repository.py` module convention; keeps the router thin and logic unit-testable)
- `backend/app/api/v1/portfolio.py`
- `backend/tests/unit/test_portfolio_reader.py`
- `backend/tests/integration/test_portfolio_api.py`

**Modify (backend):**
- `backend/app/main.py` — import + `app.include_router(portfolio_router, prefix=API_V1_PREFIX)` (one line each, matching the existing block)

**Create (frontend):**
- `frontend/src/lib/portfolio.ts`
- `frontend/src/lib/portfolio.test.ts`
- `frontend/src/app/portfolio/page.tsx`

**Modify (frontend, pure nav anchors only):**
- `frontend/src/app/backtests/page.tsx`, `frontend/src/app/signals/page.tsx`, `frontend/src/app/alerts/page.tsx`

**Not touched:** all existing schema/router/model/broker/store files, migrations, `conftest.py`, `pyproject.toml`, `package.json`, `test_permissions_matrix.py` (its helper pattern is *reused* by the new test file).

### 2.14 Test requirements

- **Backend unit** (`tests/unit/test_portfolio_reader.py`): aggregation/join/mapping over fake row objects (dates, Decimal→float, status casts, empty-ledger summary zeros, decision_id None). Pure, no DB — matches `test_ledger_broker.py`/`test_ledger_safe_mode.py` style.
- **Backend integration** (`tests/integration/test_portfolio_api.py`): seed ledger rows (via `LedgerBroker`+`PostgresLedgerStore` in the scratch DB — write path already proven in `test_ledger_persistence.py`), then: each endpoint `200` + shape assertions; `limit` clamping; status filters; ordering (§2.6); anonymous → `401`; viewer+ success; posts/puts absent. **Read-only proof:** capture counts before/after each GET (and a snapshot checksum) and assert unchanged.
- **Frontend (vitest):** `portfolio.test.ts` normalizers + empty-input handling.
- **Gates (all must pass):** `ruff` (E,W,F,I,UP,B,SIM; line-length 100), `mypy` strict on `app`, unit suite ≥ 90% coverage (existing omit list untouched; reader is/will-be covered), full integration suite, `vitest run`, `eslint .`, `next build`.

### 2.15 Risks / regressions

- **Scope creep** — the dominant risk; enforced by §2.18 out-of-scope list and the milestone being observe-only.
- **Safety regression** — a read slip (e.g., accidental write or broker instantiation) would break the SAFE MODE contract; mitigated by import discipline and the count/checksum read-only integration assertions.
- **Empty data UX** — SAFE MODE + no executor ⇒ tables empty; page must not present "no activity" as an error. Covered by summary zeros/`as_of=None` + empty-state messages modeled on the Backtests page.
- **`decision_id` nullability** — SET NULL on decision deletion; frontend handles `null` (formatter fallback "—", pattern already used for optional fields).
- **`Numeric`→`float` precision** — identical, already-accepted trade-off in `backtests.py`; read-only so no determinism impact.
- **Existing-page nav edits** — touch 3 live pages; limited to inert anchor insertion; no logic changes.
- **Cross-lib imports** — portfolio lib importing chart helpers from `backtests.ts` is a minor naming smell; documented alternative (extract shared module) deferred.

### 2.16 Minimal vertical slice

The four GETs (§2.6) + reader + one frontend page + lib/tests + nav anchors. That slice is *complete*: it displays every ledger fact Phase B promises (positions, orders, equity history, summary) and exercises the full auth/RBAC/safety/test path with the smallest possible surface.

### 2.17 Recommended implementation sequence

1. `backend/app/schemas/portfolio.py` — response models.
2. `backend/app/data/portfolio_reader.py` + `tests/unit/test_portfolio_reader.py`.
3. `backend/app/api/v1/portfolio.py`; register in `main.py`.
4. `backend/tests/integration/test_portfolio_api.py` (seed + RBAC + read-only proof).
5. `frontend/src/lib/portfolio.ts` + `portfolio.test.ts`.
6. `frontend/src/app/portfolio/page.tsx` + nav-anchor edits (backtests/signals/alerts).
7. Full gate run: ruff, mypy, pytest (unit+integration), vitest, eslint, `next build`; final read-only git review (`git status` list must equal §2.13).

### 2.18 Out of scope (explicit)

- Executor worker; automatic PAPER execution; wiring decisions → `LedgerBroker` (Phase C).
- Live trading, real broker connectivity, broker adapter, order routing.
- Any write to ledger tables from Phase B code; changing trading/risk/decision logic or `PaperBroker` math; new migrations/tables/enums.
- New Redis streams/WS topics/realtime portfolio push (goes with the reserved `fills` topic + executor phase).
- Reconciling decision-derived exposure (`load_active_paper_snapshot`) with ledger snapshots; per-user/multi-account portfolio isolation; admin-only gating; CSV/export; auto-refresh beyond page load.
- This milestone performs **no commit, tag, or push** unless explicitly requested afterwards.

### 2.19 Acceptance criteria

- All four `GET /api/v1/portfolio/*` endpoints exist, viewer+, read-only; anonymous → `401`; no mutating routes.
- Zero DB/migration changes; SAFE MODE mode flag and existing behavior unchanged; read-only proofs green.
- New unit + integration + vitest suites pass; existing 481 unit (≥90%) + 105 integration still pass; ruff/mypy/eslint/`next build` clean.
- Portfolio page renders summary cards, Positions, Orders, equity+drawdown SVG charts; truthful empty state in SAFE MODE; observe-only pill and mode badge present; nav links added.
- Working-tree changes exactly match §2.13.
- Audit completes with STOP — this document is the only artifact produced.

---

*This audit was written after reading: ledger ORM & broker/store, `api/v1` routers (backtests, market, decisions, risk, realtime, admin), `api/deps.py`, `main.py`, `schemas/decisions.py`/`common.py`, `decision_repository`, frontend `lib/*.ts` (+tests), `app/backtests/page.tsx`, `layout.tsx`, `package.json`, and `tests/integration/test_permissions_matrix.py`. All file paths checked to exist on disk.*