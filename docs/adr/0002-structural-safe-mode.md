# ADR-0002: Structural SAFE MODE

- **Status:** Accepted (2026-08-24)
- **Context:** The system analyzes markets and produces trade decisions, which makes any
  accidental live-execution path an unacceptable risk. Safety must not depend on discipline
  (“we promise not to flip a switch”) but on structure.
- **Decision:** SAFE MODE is enforced at five independent layers — config validation
  (`TRADING_MODE` only accepts `safe`), code shape (only `PaperBroker` implements the
  broker interface), orchestrator guard, startup assertion + surfaced mode in health/UI,
  and a CI safety regression suite. Full contract: [`../safe-mode.md`](../safe-mode.md).
- **Consequences:** Adding real-money execution requires superseding this ADR with explicit
  owner approval and re-designing all five layers. Until then, no configuration value,
  dependency, or code path can route orders outside the paper broker.
