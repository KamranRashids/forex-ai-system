# ADR-0005: Product scope, users, and license

- **Status:** Accepted (2026-08-24)
- **Context:** Early multi-user support would add auth/admin complexity before the core
  pipeline exists; licensing should be decided before first publication.
- **Decision:** Release v1 as a **single-user** system (one operator account), while keeping
  user identity behind an abstraction so multi-user/RBAC can be layered on later without a
  schema rewrite of domain tables. Instrument universe starts with the seven majors listed
  in ADR-0003; timeframes start at M15/H1/H4. The project is licensed under the **MIT
  license**.
- **Consequences:** Faster path to a working paper-trading loop; future multi-user work is
  additive (auth tables + ownership columns) rather than structural.
