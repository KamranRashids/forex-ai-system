# SAFE MODE Contract

> **Absolute rule:** this system performs **paper trading only**. It must never place,
> route, or simulate-toward a live brokerage order. Live order execution does not exist
> in the codebase — there is no flag that enables it.

## Enforcement layers

SAFE MODE is enforced structurally, at five independent layers. Removing it would require
touching all five, plus a signed-off governance change (new ADR + owner approval).

| Layer | Mechanism | Status (Phase 0) |
|---|---|---|
| **L1 — Config** | `TRADING_MODE` accepts only `safe`. Any other value raises and halts startup (`backend/app/main.py::validate_safe_mode`). | ✅ active |
| **L2 — Code** | Only a `PaperBroker` will ever implement the `BrokerAdapter` interface; no live-broker module exists to enable. | planned Phase 5 |
| **L3 — Orchestrator guard** | Decision pipeline hard-checks the mode before publishing any trade intent. | planned Phase 5 |
| **L4 — Startup assertion** | Boot logs `SAFE MODE ACTIVE`; `/health/live` and `/` expose the effective mode; UI shows a persistent badge. | ✅ active |
| **L5 — Tests / CI** | Regression suite: config rejection tests (running today) will grow into full pipeline assertions that zero non-paper orders can be emitted. | partial (4 safety tests) |

## How to verify

```bash
curl -s http://localhost:8000/health/live      # {"status":"ok","mode":"safe"}
docker compose exec api printenv TRADING_MODE  # safe
```

Frontend dashboard displays an amber “SAFE MODE — Paper trading only” badge sourced from
static truth until server-driven status lands in Phase 1+.

## Kill switch

To halt all analysis/decisioning immediately:

```bash
make dev-down        # stops api, workers, web, datastores
```

## Escalation policy

Any request to introduce real-money capability must be refused in code review unless:
1. A new ADR supersedes ADR-0002 with explicit owner approval,
2. All five layers are re-designed deliberately, and
3. The SAFE MODE regression suite is updated first.

Until then, “live” is not a supported value anywhere in configuration.
