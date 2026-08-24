# ADR-0003: Provider-independent market data

- **Status:** Accepted (2026-08-24)
- **Context:** Market-data vendors differ in APIs, rate limits, and reliability; tests need
  reproducible data; vendor lock-in would be costly.
- **Decision:** All ingestion code depends on a `DataProvider` interface (introduced in
  Phase 2), never on a concrete vendor SDK. The primary provider is the **OANDA v20
  practice API** (`OANDA_ENV=practice` is the only permitted value); a deterministic
  **synthetic** provider serves development, CI, and demos without credentials. Additional
  providers plug in later behind the same interface.
- **Initial universe:**
  - Pairs: `EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD`
  - Timeframes: `M15, H1, H4` — designed so `M5` and `D1` can be enabled later by
    configuration only.
