# ADR-0001: Monorepo and toolchain

- **Status:** Accepted (2026-08-24)
- **Context:** A solo-operated system with a Python backend and a TypeScript frontend needs
  low-friction cross-cutting changes, one CI entry point, and reproducible local runs.
- **Decision:** Single monorepo at the project root (`backend/`, `frontend/`, `infra/`,
  `docs/`). GNU Make drives workflows; Docker Compose v2 runs all services; GitHub Actions
  provides split CI pipelines per area plus image builds; pre-commit enforces ruff, mypy,
  and hygiene hooks.
- **Consequences:** One clone is enough to run everything; version coupling between API and
  dashboard is explicit. If a future multi-repo split is needed, boundaries already exist
  as directories.
