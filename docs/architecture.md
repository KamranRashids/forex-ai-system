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

## Operating rules

1. All timestamps UTC.
2. Every durable fact lands in PostgreSQL; Redis carries ephemeral coordination only.
3. Agents communicate via events, never by importing each other.
4. Any change that could enable order routing outside `PaperBroker` is rejected by design — see [`safe-mode.md`](safe-mode.md).
