# Multi-Agent Forex AI System — Implementation Plan

> Status: **PROPOSED — awaiting approval** · Version: 1.0 · Date: 2026-08-24
> Target directory: `/home/kamran/projects/forex-ai-system` (currently **empty** — verified, no files will be overwritten)

---

## Table of Contents

1. [Project Overview & Goals](#1-project-overview--goals)
2. [Guiding Principles & SAFE MODE Contract](#2-guiding-principles--safe-mode-contract)
3. [System Architecture](#3-system-architecture)
4. [Technology Stack](#4-technology-stack)
5. [Repository / Directory Structure](#5-repository--directory-structure)
6. [Dependencies](#6-dependencies)
7. [Containerization Plan](#7-containerization-plan)
8. [Environment Variables](#8-environment-variables)
9. [Database Design](#9-database-design)
10. [Real-Time & Messaging Design](#10-real-time--messaging-design)
11. [Observability: Logging, Metrics, Error Handling](#11-observability-logging-metrics-error-handling)
12. [Security Considerations](#12-security-considerations)
13. [Testing Strategy](#13-testing-strategy)
14. [Implementation Phases](#14-implementation-phases)
15. [Risks & Mitigations](#15-risks--mitigations)
16. [Open Questions Requiring Your Decision](#16-open-questions-requiring-your-decision)
17. [Approval Checklist](#17-approval-checklist)

---

## 1. Project Overview & Goals

A production-quality, multi-agent AI system that analyzes the Forex market and produces
trade decisions — operating exclusively in **paper-trading / backtesting mode** until
explicitly and deliberately upgraded (out of scope for v1).

### Goals

| # | Goal |
|---|------|
| G1 | Ingest reliable FX market data (multi-provider, resumable, gap-aware) |
| G2 | Run specialized analysis agents (technical, fundamental/news, sentiment, regime) |
| G3 | Aggregate agent intelligence into risk-checked trade decisions |
| G4 | Execute decisions **only** against a realistic simulated (paper) broker |
| G5 | Reproduce any paper-trade decision identically in the backtester (same signal path) |
| G6 | Expose everything through a secure FastAPI + WebSocket backend and a Next.js dashboard |
| G7 | Be observable, tested, documented, and reproducible via Docker Compose |
| G8 | Enforce SAFE MODE at multiple independent layers |

### Non-Goals (v1)

- Live/real-money order execution (no live broker adapter will exist in the codebase)
- Multi-tenant SaaS (single-operator system with role-based users)
- Options/crypto/equities (FX spot pairs only)
- Mobile apps (responsive web only)

---

## 2. Guiding Principles & SAFE MODE Contract

### Principles

1. **Safety first** — SAFE MODE is a *structural* property, not a toggle we promise not to flip.
2. **Deterministic where possible** — indicator math, risk sizing, and backtests must be reproducible (seeded, timestamped, versioned).
3. **Graceful degradation** — every LLM-dependent agent has a deterministic fallback; missing API keys disable features, never crash the system.
4. **Async-first** — all I/O (DB, Redis, HTTP, WS) is non-blocking (`asyncio`).
5. **Event-driven core** — agents communicate over Redis Streams; the API is a thin window onto that world.
6. **Everything auditable** — every decision stores its full input snapshot and rationale.
7. **Boring technology** — Postgres, Redis, FastAPI, Next.js. No exotic infrastructure.

### SAFE MODE Contract (enforced at 5 independent layers)

| Layer | Mechanism |
|-------|-----------|
| L1 Config | `TRADING_MODE` env var accepts only `safe`. Any other value fails startup validation in v1. |
| L2 Code | Only a `PaperBroker` implements the `BrokerAdapter` interface. There is **no** live-broker module to enable. |
| L3 Orchestrator | Decision pipeline hard-checks `settings.trading_mode == SAFE` before publishing any `TRADE_INTENT`; otherwise it raises and halts the cycle. |
| L4 Startup | On boot, workers log a prominent banner and refuse to start if mode config is invalid; the effective mode is surfaced in `/health` and the UI. |
| L5 Tests/CI | A regression test asserts that (a) no module matching live-execution patterns exists/importable, (b) end-to-end flows never emit orders outside the paper broker. |

---

## 3. System Architecture

### 3.1 High-Level View

```
                        ┌─────────────────────────────────────────────────────────┐
                        │                      DOCKER COMPOSE                     │
                        │                                                         │
 External Providers     │  ┌────────────┐      ┌──────────────────────────────┐   │
 ┌───────────────┐      │  │            │      │        WORKER(S)             │   │
 │ OANDA/Twelve  │──────┼─▶│  FASTAPI   │◀────▶│  (agent-runtime, same image) │   │
 │ Data (REST+WS)│      │  │    API     │      │                              │   │
 └───────────────┘      │  │            │      │ ┌──────────────────────────┐ │   │
 ┌───────────────┐      │  │ • REST v1  │      │ │ Data Ingestion Service   │ │   │
 │ News/Econ     │──────┼─▶│ • WS hub   │      │ ├──────────────────────────┤ │   │
 │ Calendar APIs │      │  │ • Auth     │      │ │ AGENT POOL               │ │   │
 └───────────────┘      │  │ • metrics  │      │ │ • Technical   • Regime   │ │   │
 ┌───────────────┐      │  └─────┬──────┘      │ │ • Fundament. • Sentiment │ │   │
 │ LLM Provider  │◀────────────┼─────────────┼─│ • Risk        • Orchestr.│ │   │
 │ (OA/Anthropic │      │        │             │ ├──────────────────────────┤ │   │
 │  /Ollama/fall)│      │        ▼             │ │ Paper Broker / Executor  │ │   │
 └───────────────┘      │  ┌────────────┐      │ ├──────────────────────────┤ │   │
                        │  │ PostgreSQL │      │ │ Scheduler (APScheduler)  │ │   │
                        │  │   16       │      │ └──────────────────────────┘ │   │
                        │  └────────────┘      └───────────────┬──────────────┘   │
                        │  ┌────────────┐                      │                  │
                        │  │  Redis 7   │◀─────────────────────┘ (Streams/PubSub)  │
                        │  └────────────┘                                          │
                        │                                                          │
                        │  ┌────────────┐   ┌──────────┐   (profiles: monitoring)  │
                        │  │  Frontend  │   │  NGINX   │   Prometheus / Grafana    │
                        │  │ (Next.js)  │───│ (prod)   │                           │
                        │  └────────────┘   └──────────┘                           │
                        └─────────────────────────────────────────────────────────┘
```

### 3.2 Components

| Component | Process | Responsibility |
|-----------|---------|----------------|
| **API** | `uvicorn` (FastAPI) | REST + WebSocket, auth, CRUD, serving dashboards data, SSE/WS fan-out from Redis |
| **Worker: ingest** | asyncio loop | Pulls/stream prices, normalizes, writes candles to PG, caches latest quotes in Redis, publishes tick/bar events |
| **Worker: agents** | asyncio loop(s) | Subscribes to bar/event streams, runs agent cycles on schedule/tick, emits `AgentSignal`s |
| **Worker: orchestrator** | asyncio loop | Consumes signals, applies regime-weighted fusion, consults risk agent, emits decisions/intents |
| **Worker: executor (paper)** | asyncio loop | Consumes intents → simulates fills → maintains positions/equity → publishes fills |
| **Backtester** | CLI (Typer) + optional job | Event-driven replay engine reusing the exact agent/decision code path |
| **Monitor** | within API/workers | Staleness detection, heartbeat checks, circuit breakers on providers, alert events |
| **Frontend** | Next.js (Node) | Dashboard: charts, signals, decisions, positions, news/sentiment, admin/settings |

### 3.3 Agent Model

Every agent implements one interface (`agents/base.py`):

```python
class BaseAgent(ABC):
    id: str                     # "technical", "regime", ...
    version: str                # bumped when logic changes (stored with every signal)

    async def analyze(self, ctx: AnalysisContext) -> AgentSignal: ...
```

`AgentSignal`: `{ agent_id, version, pair, timeframe, direction ∈ {LONG, SHORT, FLAT}, confidence ∈ [0,1], rationale, features (JSONB), created_at, valid_until }`.

**Decision fusion (orchestrator):**
1. Gather fresh signals (staleness-checked) for `(pair, timeframe)`.
2. Apply **regime-conditional weight matrix** (e.g., trending regime boosts technical weight; range regime boosts mean-reversion/technical bands; high-impact-news window suppresses all weights).
3. Weighted score + agreement heuristics → candidate action or `NO_TRADE`.
4. **Risk gate**: candidate → position size (fixed-fractional vol targeting), correlation/exposure caps, drawdown brake, min RR check. Risk may veto.
5. Emit `TRADE_INTENT {pair, side, size, entry_hint, sl, tp, ttl, rationale, snapshot_id}` → paper executor only.

### 3.4 Data Flow (per closed candle, e.g., M15 EURUSD)

```
Provider WS/REST → Ingest → PG(candles) + Redis(price cache)
                          → Stream: bars.closed.{pair}.{tf}
                                ├─▶ Technical agent ──┐
                                ├─▶ Regime agent ─────┤
   News/calendar poll ────────▶│                      ├─▶ Stream: agent.signals
   (Fundamental agent)         ├─▶ Fundamental agent ─┤        (consumer group)
   Social/news (Sentiment) ────┴─▶ Sentiment agent ───┘              │
                                                                     ▼
                                                        Orchestrator (fusion + risk)
                                                                     │
                                              Stream: decisions / intents
                                                                     ▼
                                                     Paper Executor → PG(orders/positions)
                                                                     │
                                     Redis PubSub ◀─ fills/events ───┘ → WS → Frontend
```

### 3.5 LLM Strategy

- One `LLMClient` protocol; adapters: **OpenAI**, **Anthropic**, **Ollama (local)**, **RuleBasedFallback**.
- Used by: Fundamental agent (event impact summaries), Sentiment agent (headline scoring), Orchestrator (rationale synthesis — optional).
- Each agent has an independent feature flag + provider override; if unavailable/expensive → deterministic fallback (economic-calendar proximity model; finance-lexicon scorer). The system is fully functional with zero LLM keys.

---

## 4. Technology Stack

| Layer | Choice | Rationale | Alternative considered |
|-------|--------|-----------|------------------------|
| Language (backend) | Python 3.12 | Ecosystem for quant/data/AI | 3.11 fine; 3.12 stable |
| API framework | FastAPI + Uvicorn | Async, typed, OpenAPI auto-docs | Litestar, Django-Ninja |
| DB | PostgreSQL 16 (+ optional TimescaleDB later) | Relational integrity; JSONB for features/signals | ClickHouse (overkill v1) |
| ORM / migrations | SQLAlchemy 2.0 (async) + Alembic | Standard, typed, mature | Tortoise, raw SQL |
| Cache/bus | Redis 7 (Streams, Pub/Sub, cache) | Lightweight messaging + caching in one | Kafka/RabbitMQ (heavy for v1) |
| Scheduling | APScheduler (inside workers) | Cron-style with market-hours calendar | Celery beat (heavier), Temporal (overkill) |
| Data/indicators | pandas, NumPy, pandas-ta | Pure-Python TA (no TA-Lib C dep) | TA-Lib (faster, harder install), vectorbt |
| HTTP client | httpx (+ tenacity retries) | Async, mockable (respx) | aiohttp |
| Validation/config | Pydantic v2 + pydantic-settings | Typed env config, schema validation | python-decouple |
| Auth | OAuth2 password flow + JWT (PyJWT), Argon2 hashing | Simple, standard, revocable via rotation | Authlib/OAuth social (later) |
| Logging | structlog (JSON) + stdlib interop | Correlated, machine-parseable | loguru |
| Metrics | prometheus-client + FastAPI instrumentator | Grafana-ready | OpenTelemetry-first (later phase) |
| Errors (opt) | sentry-sdk (env-gated) | Optional SaaS dependency | — |
| CLI | Typer | Consistent with FastAPI UX | argparse/click |
| Frontend | Next.js 15 (App Router) + TypeScript | SSR/hydration, ecosystem | Vite+React SPA |
| UI styling | Tailwind CSS 4 + shadcn/ui patterns | Fast, consistent | MUI, Chakra |
| Charts | TradingView Lightweight Charts | Purpose-built candlestick lib | Recharts (not for candles) |
| Server-state | TanStack Query + Zustand (UI state) | Caching/refetch discipline | Redux Toolkit |
| Forms | react-hook-form + zod | Typed validation shared shape | Formik |
| FE tests | Vitest + Testing Library + MSW; Playwright (e2e) | Modern, fast | Jest |
| Containers | Docker + Compose v2, multi-stage builds | Reproducible dev/prod parity | K8s (later) |
| Reverse proxy (prod) | NGINX (TLS termination) | Battle-tested | Traefik/Caddy |
| CI | GitHub Actions | Native to GitHub repo target | GitLab CI |

---

## 5. Repository / Directory Structure

Monorepo (backend + frontend + infra together — simplest sane default for a solo/small team):

```
forex-ai-system/
├── IMPLEMENTATION_PLAN.md          # this file
├── README.md                       # quickstart, architecture summary, SAFE MODE notice
├── LICENSE                         # MIT (TBD with you)
├── .gitignore
├── .editorconfig
├── .env.example                    # every var, documented, no secrets
├── Makefile                        # make dev / test / lint / migrate / seed ...
├── docker-compose.yml              # dev stack (PG, Redis, api, worker, frontend)
├── docker-compose.prod.yml         # prod overlay (nginx, TLS, resource limits)
├── .github/
│   ├── workflows/
│   │   ├── backend-ci.yml
│   │   ├── frontend-ci.yml
│   │   └── docker-build.yml
│   └── dependabot.yml
├── docs/
│   ├── architecture.md
│   ├── safe-mode.md
│   ├── runbook.md                  # ops procedures, incident steps
│   ├── api-guide.md
│   └── adr/                        # architecture decision records
│       ├── 0001-monorepo.md
│       └── ...
├── backend/
│   ├── Dockerfile                  # multi-stage, non-root
│   ├── pyproject.toml              # deps + tool config (ruff, mypy, pytest)
│   ├── alembic.ini
│   ├── .dockerignore
│   ├── app/
│   │   ├── main.py                 # ASGI entrypoint (api)
│   │   ├── worker_main.py          # worker entrypoint (role via env: ingest|agents|executor)
│   │   ├── cli.py                  # Typer: backfill, backtest, createuser, seed...
│   │   ├── core/
│   │   │   ├── config.py           # pydantic-settings, SAFE MODE validation
│   │   │   ├── logging.py          # structlog setup, redaction filter
│   │   │   ├── security.py         # JWT, password hashing, permissions
│   │   │   ├── errors.py           # exception hierarchy + handlers
│   │   │   └── constants.py
│   │   ├── api/
│   │   │   ├── deps.py             # auth deps, session deps, pagination
│   │   │   └── v1/
│   │   │       ├── auth.py  users.py  market.py  candles.py
│   │   │       ├── signals.py  decisions.py  portfolio.py
│   │   │       ├── news.py  backtests.py  system.py
│   │   │       └── ws.py           # WebSocket hub
│   │   ├── models/                 # SQLAlchemy models (mirrors §9)
│   │   ├── schemas/                # Pydantic DTOs (request/response)
│   │   ├── db/
│   │   │   ├── session.py  base.py
│   │   │   └── migrations/         # alembic versions
│   │   ├── data/
│   │   │   ├── providers/
│   │   │   │   ├── base.py         # DataProvider protocol
│   │   │   │   ├── oanda.py  twelve_data.py  alpha_vantage.py
│   │   │   │   └── synthetic.py    # deterministic fake feed for tests/demo
│   │   │   ├── ingest.py           # normalization, gap detection, backfill
│   │   │   ├── calendar.py         # economic calendar ingestion
│   │   │   └── timeframes.py       # tf math, session/market-hours utils
│   │   ├── agents/
│   │   │   ├── base.py             # BaseAgent, AgentSignal, AnalysisContext
│   │   │   ├── registry.py
│   │   │   ├── technical.py
│   │   │   ├── fundamental.py
│   │   │   ├── sentiment.py
│   │   │   ├── regime.py
│   │   │   ├── risk.py
│   │   │   └── orchestrator.py
│   │   ├── llm/
│   │   │   ├── client.py           # LLMClient protocol + router
│   │   │   ├── openai_client.py  anthropic_client.py  ollama_client.py
│   │   │   ├── prompts/            # versioned prompt templates
│   │   │   └── fallback/           # deterministic scorers
│   │   ├── trading/
│   │   │   ├── broker_base.py      # BrokerAdapter protocol
│   │   │   ├── paper_broker.py     # ONLY implementation in v1
│   │   │   ├── portfolio.py        # positions, equity, margin sim
│   │   │   ├── costs.py            # spread/slippage/commission/swap models
│   │   │   └── safety.py           # SAFE MODE guard utilities
│   │   ├── backtest/
│   │   │   ├── engine.py           # event-driven replay
│   │   │   ├── dataprovider.py     # historical feed adapter
│   │   │   ├── report.py           # metrics: Sharpe, Sortino, DD, PF, MAE/MFE
│   │   │   └── walkforward.py
│   │   ├── bus/
│   │   │   ├── streams.py          # Redis Streams wrappers, consumer groups
│   │   │   ├── topics.py           # canonical topic names
│   │   │   └── events.py           # event envelope schemas
│   │   ├── workers/
│   │   │   ├── ingest_worker.py
│   │   │   ├── agent_worker.py
│   │   │   ├── orchestrator_worker.py
│   │   │   ├── executor_worker.py
│   │   │   └── scheduler.py        # APScheduler jobs, market-hours aware
│   │   ├── monitor/
│   │   │   ├── staleness.py  heartbeats.py  breakers.py
│   │   │   └── alerts.py
│   │   └── utils/                  # time, math, ids, decorators
│   ├── scripts/                    # dev-only shell/py helpers
│   └── tests/
│       ├── conftest.py             # fixtures: async session, redis faker, app client
│       ├── unit/                   # agents, risk math, costs, fusion, timeutils
│       ├── integration/            # api↔pg↔redis, providers via respx, executor
│       ├── backtest/               # determinism, scenario replays
│       └── safety/                 # SAFE MODE regression suite (L5 above)
├── frontend/
│   ├── Dockerfile                  # standalone Next.js build, non-root
│   ├── package.json  package-lock.json
│   ├── next.config.mjs  tsconfig.json
│   ├── .eslintrc.cjs  postcss.config.mjs  tailwind.config.ts
│   ├── public/
│   └── src/
│       ├── app/                    # (routes) login, dashboard, pairs, decisions,
│       │                           #  backtests, settings, admin
│       ├── components/             # charts, tables, signal cards, layout, ui/
│       ├── lib/                    # api client (typed), ws client, auth store
│       ├── hooks/  stores/  types/
│       └── tests/                  # vitest + msw handlers
├── infra/
│   ├── nginx/nginx.conf            # prod reverse proxy template
│   ├── prometheus/prometheus.yml
│   ├── grafana/provisioning/       # datasources + dashboards as code
│   └── postgres/init/              # extensions, roles (dev bootstrap)
└── scripts/
    ├── bootstrap.sh                # one-command dev up
    └── backup_restore.md           # pg_dump/restore procedure
```

> Note: I will generate this tree incrementally, phase by phase — not all at once.

---

## 6. Dependencies

### 6.1 Backend — runtime (`backend/pyproject.toml`)

| Package | Purpose |
|---------|---------|
| `fastapi`, `uvicorn[standard]` | API + ASGI server (gunicorn added for prod supervising) |
| `sqlalchemy[asyncio]>=2.0`, `alembic`, `asyncpg` | Persistence + migrations |
| `redis>=5` | Cache, Streams, Pub/Sub (async client) |
| `httpx`, `tenacity` | Provider/news/LLM calls with retry/backoff |
| `websockets` | Consuming provider WS feeds |
| `pydantic>=2`, `pydantic-settings` | Schemas + typed configuration |
| `pandas`, `numpy`, `pandas-ta` | Candle frames + indicators |
| `python-dateutil`, `tzdata` | Time/timeframe math (UTC everywhere) |
| `apscheduler<4` | Scheduled jobs in workers |
| `structlog` | Structured JSON logging |
| `pyjwt[crypto]`, `argon2-cffi` | Auth tokens + password hashing |
| `prometheus-client`, `asgi-correlation-id` | Metrics + request correlation IDs |
| `typer` | Management CLI |
| `slowapi` (or small custom limiter) | API rate limiting |
| `sentry-sdk` *(optional)* | Error tracking (enabled only if `SENTRY_DSN` set) |
| `python-multipart` | Login form parsing |

### 6.2 Backend — dev/test

`pytest`, `pytest-asyncio`, `pytest-cov`, `respx` (mock httpx), `freezegun`, `factory-boy`,
`fakeredis[lua]`, `ruff` (lint+format), `mypy`, `pre-commit`, `hypothesis` (risk-math properties),
`locust` (light load smoke, optional), `pip-audit` (dependency CVE scan), `types-*` stubs.

### 6.3 Frontend (`frontend/package.json`)

| Package | Purpose |
|---------|---------|
| `next`, `react`, `react-dom`, `typescript` | App core |
| `tailwindcss`, `postcss`, `autoprefixer` | Styling |
| `lightweight-charts` | Candlestick/indicator charts |
| `@tanstack/react-query`, `zustand` | Server state + local state |
| `react-hook-form`, `zod` | Forms/validation |
| `ky` (thin fetch wrapper) | API client |
| `reconnecting-websocket` | Resilient WS to backend |
| `lucide-react` | Icons |
| Dev: `eslint`, `eslint-config-next`, `vitest`, `@testing-library/react`, `msw`, `playwright` | Quality/tests |

Version pinning: exact/minor-pinned versions recorded at scaffold time; Dependabot keeps them fresh.

---

## 7. Containerization Plan

All runtime components run in Docker; only your IDE runs on the host. Dev uses bind-mounts + reload; prod uses built images.

| Service (compose) | Image | Runs in Docker? | Notes |
|---|---|---|---|
| `postgres` | `postgres:16-alpine` | ✅ | Volume `pgdata`; init scripts mount extensions/roles |
| `redis` | `redis:7-alpine` | ✅ | AOF persistence for stream durability |
| `api` | build `./backend` (multi-stage) | ✅ | Uvicorn --reload in dev; gunicorn+uvicorn.workers in prod; non-root user |
| `worker` | same backend image | ✅ | Role selected via `WORKER_ROLE=ingest\|agents\|orchestrator\|executor` (can scale replicas) |
| `frontend` | build `./frontend` (node multi-stage → standalone) | ✅ | Talks to API via internal network / nginx in prod |
| `nginx` | `nginx:alpine` | ✅ (prod profile) | TLS termination, gzip, security headers |
| `prometheus`, `grafana` | official images | ✅ (profile `monitoring`) | Dashboards provisioned as code |
| `adminer`/`pgadmin` | official | ✅ (profile `debug`, dev only) | Never in prod |

Image hygiene: pinned base digests, `.dockerignore`, non-root `UID 10001`, healthchecks for all services,
`restart: unless-stopped`, resource limits in prod overlay. Compose **profiles**:
default (core stack), `monitoring`, `debug`.

---

## 8. Environment Variables

Single `.env` at repo root (compose) + `backend/.env` overrides; `.env.example` documents all. Grouped:

### Core / Safety
| Var | Example | Notes |
|---|---|---|
| `APP_ENV` | `dev` \| `prod` | affects defaults (CORS, debug) |
| `TRADING_MODE` | `safe` | **v1 accepts only `safe`**; anything else fails startup |
| `SECRET_KEY` | 64-char random | JWT signing; rotate via kid scheme |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `14` | rotation + reuse detection |
| `CORS_ORIGINS` | `http://localhost:3000` | strict allowlist |
| `LOG_LEVEL` | `INFO` | |
| `SENTRY_DSN` | *(empty)* | optional; empty disables |

### Datastores
| Var | Example |
|---|---|
| `POSTGRES_HOST` / `PORT` / `DB` / `USER` / `PASSWORD` | `postgres` / `5432` / `forex_ai` / `forex` / strong-random |
| `DATABASE_URL` | composed or explicit async DSN |
| `REDIS_URL` | `redis://redis:6379/0` |

### Market Data
| Var | Notes |
|---|---|
| `MARKET_DATA_PROVIDER` | `oanda` \| `twelve_data` \| `alpha_vantage` \| `synthetic` (default for dev/tests) |
| `MARKET_DATA_SYMBOLS` | `EURUSD,GBPUSD,USDJPY,AUDUSD` |
| `MARKET_DATA_TIMEFRAMES` | `M5,M15,H1,H4,D1` |
| `OANDA_API_TOKEN`, `OANDA_ENV` | `practice` enforced in v1 |
| `TWELVEDATA_API_KEY`, `ALPHAVANTAGE_API_KEY` | alternates |
| `NEWS_API_KEY`, `FINNHUB_API_KEY` | fundamental/sentiment sources |
| `ECON_CALENDAR_SOURCE` | `finnhub` (fallback: bundled static major-events list) |

### LLM / Agents
| Var | Notes |
|---|---|
| `LLM_PROVIDER` | `openai` \| `anthropic` \| `ollama` \| `none`(fallbacks) |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `OLLAMA_BASE_URL` | provider creds/endpoints |
| `LLM_MODEL`, `LLM_MAX_TOKENS`, `LLM_DAILY_BUDGET_USD` | cost guardrails; budget breaker stops LLM calls |
| `AGENT_TECHNICAL_ENABLED`, `AGENT_FUNDAMENTAL_ENABLED`, `AGENT_SENTIMENT_ENABLED`, `AGENT_REGIME_ENABLED`, `AGENT_RISK_ENABLED` | per-agent switches (all `true` by default; fundamentals/sentiment fall back if no keys) |
| `ANALYSIS_INTERVAL_SECONDS` | agent cycle cadence per timeframe |

### Risk Parameters (defaults; adjustable in UI by admin later)
| Var | Default | Meaning |
|---|---|---|
| `RISK_PER_TRADE_PCT` | `0.5` | % equity risked per trade |
| `MAX_TOTAL_EXPOSURE_PCT` | `200` | max notional across positions (leverage cap) |
| `MAX_POSITIONS_PER_PAIR` | `1` | no stacking initially |
| `MAX_CORRELATED_EXPOSURE` | `2` | same-direction correlated pairs limit |
| `MAX_DAILY_LOSS_PCT` | `2.0` | daily circuit breaker |
| `MAX_DRAWDOWN_PCT` | `10.0` | hard equity brake |
| `MIN_REWARD_RISK` | `1.5` | reject candidates below this RR |
| `PAPER_INITIAL_BALANCE` | `100000` | starting paper equity |

### Frontend / Misc
| Var | Notes |
|---|---|
| `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_WS_URL` | browser-facing endpoints |
| `BACKEND_WORKERS` | worker concurrency |
| `TZ` | containers run `UTC` internally regardless |

Secrets policy: `.env*` gitignored; CI/prod use GitHub Environments/Actions secrets or Docker secrets later.

---

## 9. Database Design

PostgreSQL 16, UTC `timestamptz`, UUID PKs (except candles), Alembic-managed. Key tables:

| Table | Key columns | Purpose |
|---|---|---|
| `instruments` | symbol, base, quote, pip_size, contract specs, active | tradable universe |
| `candles` | (instrument_id, timeframe, ts) **unique composite**, o,h,l,c, volume, source, complete | OHLCV store; upsert-on-close semantics |
| `ticks_latest` (Redis primary, PG snapshot optional) | price/bid/ask/ts | last quote cache |
| `news_articles` | source, headline, body, url, published_at, currencies[], hash unique | dedup'd corpus |
| `economic_events` | ts, country, name, importance, actual/forecast/previous | calendar |
| `sentiment_scores` | article_id/event ref, score, label, model, version | per-item scores |
| `agent_signals` | agent_id, version, instrument, timeframe, direction, confidence, rationale, features JSONB, created_at, valid_until, run_id | full audit of every agent output |
| `decisions` | instrument, timeframe, action, score, weights_snapshot JSONB, inputs_hash, intent JSONB, status | orchestrator output + reproducibility snapshot |
| `orders_paper` | client_order_id, decision_id, side, qty, type, requested/executed price, slippage, status, costs JSONB | paper order lifecycle |
| `positions` | instrument, side, qty, avg_price, sl, tp, opened_at, closed_at, pnl | current/history positions |
| `account_snapshots` | ts, balance, equity, margin_used, open_pnl | equity curve |
| `backtest_runs` | config JSONB, data_range, seed, code_versions, metrics JSONB, status | reproducible backtests |
| `users` | email, hashed_password, role ∈ {admin,trader,viewer}, is_active, last_login | authn/authz |
| `refresh_tokens` | user_id, jti, expires_at, revoked, replaced_by | rotation/reuse detection |
| `audit_log` | actor, action, entity, before/after JSONB, ip, ts | sensitive-action trail |
| `system_settings` | key, value JSONB, updated_by | runtime-tunable params (risk knobs) |
| `provider_health` | provider, last_ok_at, consecutive_failures, breaker_state | monitor state |

Indexes: candles `(instrument_id, timeframe, ts DESC)`; signals `(instrument,timeframe,created_at DESC)`; partial index on open positions; GIN on JSONB feature columns where queried.

Retention: raw candles kept forever (small); news bodies trimmed after N months (scores retained); audit log immutable.

---

## 10. Real-Time & Messaging Design

**Redis topology**

| Channel/Stream | Type | Producer → Consumer |
|---|---|---|
| `prices.live` | Pub/Sub | ingest → API WS fan-out, executor |
| `bars.closed.{tf}` | Stream | ingest → agents (group `agents`) |
| `signals.stream` | Stream | agents → orchestrator (group `orchestrator`) |
| `decisions.stream` | Stream | orchestrator → executor (group `executor`), archiver |
| `events.fills` / `events.alerts` | Pub/Sub + PG | executor/monitor → API WS → UI |
| locks: `lock:ingest:{provider}`, idempotency keys | KV | workers |

Design notes: Streams give consumer-groups + ACK + pending-list recovery (at-least-once);
handlers are idempotent keyed on `(run_id, entity)`. Tick bursts go through Pub/Sub only
(no durability needed); durable facts always land in PG. Message envelopes carry
`schema_version` for forward compatibility. Backpressure: agents process latest-bar-per-pair,
skipping stale backlog rather than queueing unbounded work.

**WebSocket API**: `/ws/v1/stream?topics=pairs,decisions,fills,alerts` — server multiplexes
topics after auth (token via first message or short-lived ticket); ping/pong keepalive;
client resumes with last-event-id cursor where applicable.

---

## 11. Observability: Logging, Metrics, Error Handling

- **Logging**: structlog JSON to stdout; every request gets `correlation_id` (asgi-correlation-id); every agent cycle/decision carries `run_id` linking all logs+DB rows. Redaction filter scrubs tokens/keys/password fields. Dev pretty console renderer.
- **Metrics (Prometheus)**: candles ingested (counter by pair/tf), ingest lag & staleness gauge, provider error rate/breaker state, agent cycle duration + outcome, LLM calls/tokens/cost, decision counts by action, order fill stats, WS connections, queue depth (pending stream entries), process/runtime basics.
- **Dashboards**: Grafana provisioned: “System Health”, “Ingest Quality”, “Agents & Decisions”, “Paper Portfolio”.
- **Alerting (v1: in-app + log-based)**: staleness > X minutes during market hours, breaker trips, daily-loss brake hit, LLM budget exhausted → `alerts` events surfaced in UI; Slack/webhook hook left as a stub.
- **Error handling**: central exception hierarchy (`AppError` → domain errors); API returns RFC7807-style problem+json with correlation id; workers wrap cycles with structured failure capture + bounded retries (tenacity) + poison-pill quarantine (failed messages logged with payload, never crash the loop); graceful shutdown drains consumers.
- **Health**: `/health/live` (process up), `/health/ready` (DB+Redis reachable, migrations current, SAFE MODE asserted), plus component matrix in `/system/status`.

---

## 12. Security Considerations

| Area | Measures |
|---|---|
| **SAFE MODE** | 5-layer enforcement (§2); UI shows persistent “PAPER — SAFE MODE” badge sourced from server truth |
| Authentication | Argon2id hashes; short-lived JWT access (memory-only client), rotating refresh in `HttpOnly; Secure; SameSite=Lax` cookie; reuse detection revokes family; lockout/backoff on brute force |
| Authorization | RBAC: `viewer` < `trader` < `admin` (v1: admin manages users/settings; trader sees all, cannot alter safety config without admin) |
| Transport | Prod: TLS via nginx (HSTS, modern ciphers); dev: localhost only |
| Input validation | Pydantic on every boundary; zod mirrors on FE; strict query param bounds (timeframe/pair whitelists) |
| Injection | SQLAlchemy bound params only; no string SQL; Redis commands parameterized |
| XSS/CSRF | React escaping + CSP headers; JWT-not-cookie for access token mitigates CSRF; refresh-cookie endpoints additionally check Origin; state-changing API requires `X-Requested-With` |
| Rate limiting | Per-IP + per-user limits on auth and expensive endpoints |
| Secrets | Never logged (redaction filter), never in images, `.env` gitignored, example file only; prod: environment/Docker secrets; quarterly rotation noted in runbook |
| Supply chain | pip-audit + npm audit in CI; Dependabot; pinned versions; base-image digest pinning |
| Containers | Non-root UID, read-only rootfs where feasible, dropped capabilities, no `latest` tags |
| LLM-specific | Untrusted news text treated as data: strict output schemas (JSON mode/function-style parsing), length caps, prompt-injection-resistant templates, per-day spend breaker, no secrets ever passed into prompts |
| Auditability | Immutable `audit_log`; every decision links inputs snapshot + code versions (agent `version` strings) |
| Data protection | Local-only deployment by default; no PII beyond operator emails; backups encrypted-at-rest guidance in runbook |

---

## 13. Testing Strategy

**Pyramid**: broad fast unit base → focused integration → few end-to-end.

| Level | Scope | Tooling | Highlights |
|---|---|---|---|
| Unit (~70%) | Indicator math (golden values), timeframe/session math, cost & slippage models, **risk sizing formulas (property-based via hypothesis)**, regime classifier boundaries, fusion weighting matrix, JWT/hash utilities | pytest, hypothesis | Deterministic, <5s total |
| Integration (~25%) | API ↔ real PG/Redis (docker compose `test` profile or testcontainers), provider adapters via `respx` (incl. error/timeout/gap cases), ingest idempotency/upsert, executor fill simulation against synthetic feed, WS hub subscribe/auth | pytest-asyncio, httpx AsyncClient, fakeredis for edge branches | Migrations applied on scratch DB |
| Backtest correctness | Seeded synthetic series with known outcomes (e.g., pure trend → long bias), fee sensitivity, determinism: identical config+data+seed ⇒ byte-identical report | pytest | Guards the “same signal path” invariant |
| **SAFE MODE regression** | (a) grep/import-scan proves no live-broker modules; (b) config validator rejects `TRADING_MODE != safe`; (c) full pipeline run emits zero non-paper orders | pytest, marked critical | Runs in every PR |
| E2E (smoke) | Login → dashboard loads → charts render from synthetic feed → decision appears | Playwright | Few, stable paths |
| Frontend unit | Components/hooks with MSW-mocked API | Vitest + Testing Library | |
| Non-functional | `locust` smoke on API; coverage gates | Coverage ≥85% on `core/`, `agents/`, `trading/`, `backtest/`; overall ≥80% | CI-enforced |

Conventions: factories for entities; `freezegun` clock control; no network in tests (providers mocked; `synthetic` provider used for realism); markers `unit|integration|e2e|safety` with sensible CI defaults.

**CI (GitHub Actions)**: backend job (ruff→mypy→pytest+cov), frontend job (eslint→tsc→vitest), docker build job (both images compile), all on PR; nightly full integration + pip-audit/npm-audit.

---

## 14. Implementation Phases

Each phase ends with working, reviewed, tested software. Estimates assume one experienced dev, focused; ranges reflect unknowns (provider quirks, LLM tuning).

### Phase 0 — Repository Foundation & Infra Skeleton (2–3 days)
- Git init, ignore/editorconfig/license, monorepo scaffold (§5 tree with placeholders), Makefile, `.env.example`
- `docker-compose.yml` with postgres+redis (+healthchecks), backend/frontend Dockerfiles (hello-world stage), nginx template
- GitHub Actions CI skeletons, pre-commit (ruff, mypy hook, eslint), Dependabot config
- README skeleton + `docs/architecture.md` initial + ADR records for key choices
- **Exit criteria**: `make dev` boots all containers healthy; CI green on scaffold; `docs/safe-mode.md` written.

### Phase 1 — Backend Core (3–5 days)
- Settings (pydantic-settings) incl. SAFE MODE validation; structlog logging + correlation IDs; exception hierarchy + problem+json handlers
- SQLAlchemy async engine/session, Alembic baseline; models: users, refresh_tokens, audit_log, system_settings
- Auth: register/login/refresh/logout/me endpoints, RBAC deps, rate limiting on auth; CLI `createuser`
- `/health/*`, `/system/status`; OpenAPI polish
- Tests: unit + integration (auth flow, token rotation, permission matrix)
- **Exit criteria**: authenticated API over migrated DB; coverage gates active; SAFE MODE misconfig test red/green verified.

### Phase 2 — Market Data Layer (4–6 days)
- `DataProvider` protocol + OANDA practice adapter (REST history + streaming), Twelve Data adapter, `synthetic` deterministic generator
- Ingest worker: normalize → candle aggregation (M1..D1) → gap detect/backfill → upsert PG → Redis latest cache → publish `bars.closed.*`/`prices.live`
- Instruments seeding, market-hours/session calendar, provider health tracking + circuit breaker
- Admin endpoints: symbols/timeframes config, backfill trigger; CLI `backfill`
- Tests: adapter contracts (respx), gap handling, idempotent upserts, breaker transitions
- **Exit criteria**: continuous candles for ≥3 pairs across M5–D1 survive restarts and provider hiccups; staleness alerts fire correctly.

### Phase 3 — Agent Framework + Technical & Regime Agents (4–6 days)
- `BaseAgent`/`AgentSignal`/context + registry + signal persistence; agent worker loop consuming bar streams (per-pair/per-tf cadence)
- Technical agent: EMA/SMA, RSI, MACD, Bollinger, ATR, Stochastic, ADX, Donchian, pivot levels; confluence scoring → direction/confidence with rationale
- Regime agent: ADX slope/threshold, ATR-percentile volatility bucket, range-vs-trend classifier, session context; emits regime labels consumed as conditioning metadata
- Grafana “Agents & Decisions” board v0; WS broadcast of signals (API side minimal)
- Tests: indicator goldens, regime boundary fixtures, signal freshness/staleness rules
- **Exit criteria**: live synthetic-feed run shows persisted, versioned signals per closed bar with sub-second agent latency at M5.

### Phase 4 — Fundamental & Sentiment Agents (5–7 days)
- Economic-calendar ingestion (importance-tagged events) + news fetch/dedup pipeline (hash/url)
- `LLMClient` abstraction + OpenAI/Anthropic/Ollama adapters + **deterministic fallbacks** (calendar-proximity impact model; finance-lexicon sentiment) + daily-budget breaker
- Fundamental agent: event-window risk states, surprise scoring (actual vs forecast), currency-strength narrative
- Sentiment agent: per-headline scoring → rolling aggregate per currency/pair with decay
- News blackout windows fed to orchestrator config; UI-ready payloads
- Tests: parser schemas (incl. adversarial/injection-shaped payloads), fallback equivalence suites, budget breaker
- **Exit criteria**: with zero LLM keys the system still produces sensible fundamental/sentiment signals; with keys, richer rationales stored.

### Phase 5 — Risk Agent + Orchestrator + Decision Pipeline (5–7 days)
- Risk agent: vol-targeted position sizing (ATR-based stop distance), exposure/correlation caps, daily-loss & max-DD brakes, min-RR veto; outputs sized intent or veto reason
- Orchestrator worker: signal collection/staleness, regime-conditional weight matrix, agreement scoring, thresholds/hysteresis, cooldowns per pair; emits `decisions` + intents; full snapshot persistence (inputs_hash, weights, code versions)
- Paper executor worker v1: consume intents → simulated market fills w/ spread+slippage → positions/orders/account snapshots → fills events
- SAFE MODE guard utilities wired through (L3); alerts on brake trips
- Tests: fusion matrix table-tests, risk formulas (property-based), veto paths, end-to-end synthetic run producing auditable decision chain
- **Exit criteria**: autonomous paper loop running on schedule with every decision fully explainable in DB; SAFE MODE regression suite green.

### Phase 6 — Backtesting Engine (5–8 days)
- Event-driven replay engine reusing the exact agent/orchestrator/risk code path (invariant: identical signals for identical bars)
- Cost model parity with paper broker; warmup handling; seeded randomness; multi-pair portfolios
- Reporting: net/gross PnL, win-rate, profit factor, Sharpe/Sortino, max DD, exposure, per-regime breakdown; equity/drawdown curves persisted
- CLI `backtest run --range ... --pairs ... --config ...`; `backtest_runs` persistence + compare view API; walk-forward scaffolding
- Tests: determinism, scenario suites, cost sensitivity
- **Exit criteria**: reproduce a prior week’s live-paper decisions bit-for-bit in backtest; report quality sufficient to judge strategy changes.

### Phase 7 — Monitoring & Operational Hardening of Runtime (3–5 days)
- Monitor service: staleness watchdogs, heartbeats per worker, breaker orchestration, alert events → UI/log
- Graceful startup/shutdown ordering; crash-only design review (any worker restart resumes cleanly mid-stream)
- Weekend/market-closed behavior; DST-safe calendars; load smoke (locust) on ingest+WS
- Runbook v1 (common failures, recovery, backup/restore)
- **Exit criteria**: kill-any-process chaos drill recovers automatically with no duplicate fills and no lost closed-bar processing.

### Phase 8 — Complete REST + WebSocket API (4–6 days)
- Full routers: candles/aggregates, signals, decisions (with rationale expansion), portfolio/positions/account, news/events/sentiment, backtests (CRUD+results), system/admin (settings, provider status), users admin
- Pagination/filtering standards, ETags on heavy reads; WS hub topics + auth tickets + resume cursors
- OpenAPI client generation for frontend (typed SDK)
- Tests: contract tests per router, WS auth/topic isolation
- **Exit criteria**: every dashboard need servable from documented API alone; typed client checked into frontend.

### Phase 9 — Next.js Frontend (8–12 days)
- Foundation: App Router layout, Tailwind/shadcn base kit, dark theme, auth screens + token handling, route guards, error/empty/loading states, i18n-ready strings (en only v1)
- Dashboard: portfolio KPIs, equity curve, open positions/orders, recent decisions w/ rationale drawer
- Charts page: Lightweight-Charts candles + indicator overlays + regime shading + signal markers; pair/timeframe switcher
- Agents page: per-agent cards (latest signal, confidence, rationale, freshness), regime banner, news/sentiment feeds
- Backtests page: launch form, results tables/charts, run comparison
- Admin: users, settings (risk knobs), provider status, SAFE MODE banner
- Realtime wiring (WS) with reconnect + optimistic updates where safe; accessibility pass
- Tests: Vitest units, Playwright smoke (login→dashboard→charts)
- **Exit criteria**: full operator workflow usable end-to-end against synthetic feed; Lighthouse perf/a11y sanity pass.

### Phase 10 — Security, Performance & Release Hardening (3–5 days)
- Threat-model review vs §12 checklist; dependency audits clean; CSP/security headers verified; rate-limit tuning
- Perf: query EXPLAIN pass on hot endpoints, Redis cache hit review, WS fan-out benchmark
- Prod compose overlay finalized (TLS docs, backups cron script, resource limits); image scanning (trivy) in CI
- Pen-test checklist self-audit; secrets rotation runbook
- **Exit criteria**: hardening checklist signed off in `docs/runbook.md`; prod profile boots with nginx TLS.

### Phase 11 — Documentation & GitHub Readiness (2–3 days)
- README (quickstart ≤5 commands), full docs pass, ADRs finalized, CHANGELOG, issue/PR templates, branch protection suggestion, semantic-version tagging, `v0.1.0` release cut
- Demo dataset + `synthetic` replay script so reviewers can run the whole system offline
- **Exit criteria**: fresh-machine clone → `make dev` → working SAFE-MODE system with demo data, no manual steps undocumented.

**Total estimate: ≈ 46–73 focused dev-days.** Suggested checkpoint reviews after Phases 2, 5, 6, and 9.

---

## 15. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Provider API limits/outages | Data gaps → bad signals | Multi-provider abstraction, gap detector+backfill, breakers, staleness alerts |
| LLM cost/latency creep | Budget blowout, slow cycles | Daily budget breaker, caching by content-hash, fallbacks, cheap-model routing |
| Overfitting to backtests | False confidence | Walk-forward scaffolding, out-of-sample discipline, cost-parity testing, no look-ahead guards (asserted in tests) |
| Look-ahead/data-snooping bugs | Invalid results | Bar-close-only processing, timestamps UTC monotonic checks, determinism tests |
| Complexity sprawl (agents) | Hard maintenance | Single BaseAgent contract, registry, versioned signals, thin orchestrator |
| Redis as single bus dependency | Outage halts pipeline | Durable facts in PG; workers reconnect+resume from last-ACK; PG fallback polling mode (documented) |
| Solo-project bus factor | Knowledge loss | ADRs, runbook, docs-as-code from day 1 |
| Scope creep toward live trading | Safety erosion | SAFE MODE contract (§2); live adapters explicitly out-of-scope for v1 roadmap |

---

## 16. Open Questions Requiring Your Decision

1. **Primary market-data provider** — OK to standardize on **OANDA v20 practice** (needs free practice account/token) with **Twelve Data** as alternate and `synthetic` for offline dev?
2. **LLM provider** — preference among OpenAI / Anthropic / local Ollama (or “none, fallbacks only” to start)? Any monthly budget cap I should encode?
3. **Instrument universe** — start with majors `EURUSD, GBPUSD, USDJPY, AUDUSD, USDCHF, USDCAD, NZDUSD`?
4. **Timeframes priority** — M5/M15/H1/H4/D1 as planned, or different emphasis?
5. **Historical depth for backtests** — 2 years daily + 6 months intraday reasonable to start? (Provider limits apply; Dukascopy bulk import can be a later add-on.)
6. **Users** — single operator (one admin) or a couple of viewer accounts from day one?
7. **Deployment** — local Docker only for v1, with prod overlay ready-but-unexercised?
8. **License** — MIT OK for the repo?

---

## 17. Approval Checklist

- [ ] Architecture (§3) approved
- [ ] Tech stack (§4) approved
- [ ] Directory structure (§5) approved
- [ ] Dependency list (§6) approved
- [ ] Docker layout (§7) approved
- [ ] Env vars & defaults (§8) approved
- [ ] Schema sketch (§9) approved
- [ ] Security & SAFE MODE approach (§2, §12) approved
- [ ] Testing strategy (§13) approved
- [ ] Phasing & estimates (§14) approved
- [ ] Open questions (§16) answered

> **No application code has been written.** Upon approval, work begins at Phase 0.
