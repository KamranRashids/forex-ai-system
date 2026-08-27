# Architecture Overview

> Status: scaffold (Phase 0). The full proposal lives in [`IMPLEMENTATION_PLAN.md`](../IMPLEMENTATION_PLAN.md) §3;
> this page summarizes what exists today and where it is heading.

## System at a glance

```
External providers            Docker Compose network
┌───────────────────┐        ┌──────────────────────────────────────────────┐
│ OANDA practice /  │        │  api (FastAPI) ── web (Next.js)              │
│ synthetic feed    │──(P2)──▶│     │                 │                      │
└───────────────────┘        │     ▼                 ▼                      │
┌───────────────────┐        │  postgres ◀── workers/agents (P2–P7)         │
│ LLM: OpenCode Zen │◀─(P4)──│  redis (cache + streams)                     │
│ / fallbacks       │        │  nginx (prod overlay, P10 TLS)               │
└───────────────────┘        └──────────────────────────────────────────────┘
```

## Components and status

| Component | Purpose | Introduced |
|---|---|---|
| `api` | REST + WebSocket surface, auth, health | Phase 0 placeholder (`/`, `/health/live`) → real core in Phase 1 |
| `postgres` | Persistent state (candles, signals, decisions, paper orders…) | Phase 0 container; schema from Phase 1–2 |
| `redis` | Cache + event coordination (Streams/Pub-Sub) | Phase 0 container; used from Phase 2 |
| `web` | Next.js dashboard with SAFE MODE badge | Phase 0 placeholder → full UI in Phase 9 |
| Ingest worker | Provider-independent market data pipeline | Phase 2 |
| Agents (technical, regime, fundamental, sentiment) | Analysis signal producers | Phases 3–4 |
| Risk agent + orchestrator + paper executor | Decision fusion and paper-only execution | Phase 5 |
| Backtester | Deterministic replay reusing agent code path | Phase 6 |

## Key decisions (ADRs)

- ADR-0001 — Monorepo & toolchain
- ADR-0002 — Structural SAFE MODE (5 enforcement layers)
- ADR-0003 — Provider-independent market data (OANDA practice primary, synthetic fallback)
- ADR-0004 — LLM abstraction (OpenCode Zen / Ox Alpha Free first, deterministic fallbacks mandatory)
- ADR-0005 — Product scope (single-user first, MIT)

## Ingestion strategy (Phase 2)

- Providers return **native candles per timeframe** (`fetch_candles`); there is no
  tick→M1 aggregation pipeline in v1. This mirrors how OANDA serves history and keeps
  the synthetic provider simple; cross-TF aggregation can be layered later behind the
  same interface if a base-timeframe feed is ever introduced.
- The **synthetic** generator is deterministic value-noise keyed by
  `(symbol, timeframe, bucket)` — order-independent, restart-safe, and backfill-friendly.
  It is a fixture, never a forecast.
- Bars are stored with **upsert-on-close** semantics (composite PK
  `instrument_id, timeframe, ts`; `complete` may never regress). Gap detection compares
  expected buckets against returned bars per cycle for observability.
- Durable facts go to PostgreSQL; Redis carries `bars.closed.{tf}` streams,
  `prices.live` Pub/Sub quotes, latest-price cache keys, breaker state in PG, and a
  lightweight admin backfill queue drained by the ingest worker.
- The FX week model: Sunday 22:00 → Friday 22:00 UTC continuous; staleness alerts are
  suppressed while the market is closed.

## Agents (Phase 3)

- Contract (§3.3): `BaseAgent.analyze(AnalysisContext) -> AgentSignal`; every
  signal carries the agent's `version`, direction ∈ {LONG, SHORT, FLAT},
  confidence ∈ [0,1], full indicator snapshot in `features`, and a freshness
  horizon (`valid_until` = bucket + 2×tf for technical, 4×tf for regime).
- **Technical agent** scores nine documented votes (EMA cross, SMA50, MACD
  histogram, RSI midline/extremes, Bollinger breakout, Stochastic, Donchian,
  prior-day pivots) with ATR-relative dead-zones and ADX trend-gating;
  thresholds ±0.15 separate LONG/SHORT from FLAT.
- **Regime agent** emits conditioning metadata only (direction always FLAT):
  trending / weakening_trend / transitional / range from ADX level+slope,
  ATR% tercile volatility buckets, and UTC session labels.
- Persistence is idempotent: unique key `(agent_id, symbol, timeframe,
  bucket_ts)`; replays never duplicate rows and `run_id` records the first
  processing batch. Signals also publish to `signals.stream` for the Phase 5
  orchestrator; the agents consumer group applies latest-bar-per-pair
  backpressure on backlog.

## Content ingestion (Phase 4)

- News and economic-calendar ingestion behind two provider interfaces —
  `NewsProvider` (news) and `CalendarProvider` (economic calendar) — so downstream
  agents and the API depend only on normalized data, never on an external provider.
- Each interface has two adapters behind a single factory:
  `finnhub_{news,calendar}` (primary, used only when a key is configured) and
  `synthetic_{news,calendar}` (deterministic seeded/demo data, the default).
  Missing/blank/invalid/rate-limited/unavailable credentials degrade gracefully to the
  synthetic adapter.
- Normalized records carry UTC timestamps, source/provider metadata, and a stable
  dedup key (news: url-hash; calendar: provider event UID). Persistence is
  idempotent — replays never duplicate rows (migration `0004` adds
  `news_items` / `economic_events`).
- Provider tokens are used only for outbound auth and never appear in persisted
  records, API responses, or logs (`raw_payload` is sanitized to safe fields).
- **Fundamental agent** scores event-window risk states from calendar proximity +
  surprise (actual vs forecast); **sentiment agent** scores headlines via a
  finance-lexicon model → rolling per-currency/pair aggregate with decay.
- LLM reasoning is optional and gated by `LLMClient` (ADR-0004): every LLM-backed
  agent keeps a deterministic fallback (calendar-proximity impact model;
  finance-lexicon sentiment), and `LLM_DAILY_BUDGET_USD` bounds spend.

## Operating rules

1. All timestamps UTC.
2. Every durable fact lands in PostgreSQL; Redis carries ephemeral coordination only.
3. Agents communicate via events, never by importing each other.
4. Any change that could enable order routing outside `PaperBroker` is rejected by design — see [`safe-mode.md`](safe-mode.md).
