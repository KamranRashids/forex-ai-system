# ADR-0004: LLM abstraction with OpenCode Zen / Ox Alpha Free first

- **Status:** Accepted (2026-08-24)
- **Context:** Fundamental/news, sentiment, and rationale synthesis benefit from LLM
  reasoning, but models, vendors, and pricing change frequently — and the system must also
  run with no API key at all.
- **Decision:** Agents never call an LLM directly; they depend on an `LLMClient`
  abstraction (introduced in Phase 4). The initial configured provider is
  **OpenCode Zen** running the **Ox Alpha Free** model
  (`LLM_PROVIDER=opencode_zen`, `OPENCODE_ZEN_MODEL=ox-alpha-free`). Swapping providers
  later is a configuration/adapter change, not an agent rewrite. Every LLM-backed agent
  ships a deterministic fallback used when the provider is disabled, unkeyed, over budget,
  or failing.
- **Consequences:** The full pipeline is testable offline; cost is bounded by
  `LLM_DAILY_BUDGET_USD`; model quality changes are isolated behind one interface and
  versioned prompts.
