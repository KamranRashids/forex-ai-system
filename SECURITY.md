# Security Policy

## Supported & live posture

- **SAFE MODE is structural.** This system performs paper/backtest analysis only and
  never connects to a brokerage. Live order execution does not exist anywhere in the
  codebase; there is no flag that enables it (`docs/safe-mode.md`).
- The current release track is `v0.1.0` (development/staging). No production traffic
  or real funds are involved at any stage.

## Reporting a vulnerability

While this is a SAFE MODE, local-deployment project, security bugs still matter —
especially anything that could expose credentials, session tokens, customer data, or
allow unauthorized access to the API or database.

Please report privately, with as much information as available:

- Use the repository's **private vulnerability reporting** channel if enabled on the
  remote (GitHub → Security → Report a vulnerability).
- Otherwise, keep findings out of public issue titles: open a normal issue with a
  neutral title and include: affected version (`v0.1.0`), component, a minimal
  reproduction, and impact.

If you are reviewing privately, briefly notify the repository owner before or in
parallel with filing an issue.

You should not expect a bounty — this is a solo, non-commercial project.

## What to include

- Repository version (`v0.1.0`) and commit/tag under test.
- Steps to reproduce (config + environment; the stack runs fully offline with the
  `synthetic` provider and zero external keys).
- Whether the issue is in the API, workers, frontend, Docker/CI configuration, or docs.
- Any security-measure references that were (or were not) hit, e.g. rate limits,
  trusted-proxy handling, CSP/security headers (`docs/phase10-security-hardening.md`).

## Security measures (where they are documented)

| Measure | Documentation |
|---|---|
| SAFE MODE enforcement (5 layers) | `docs/safe-mode.md` |
| Threat model & self-audit checklist | `docs/threat-model.md` |
| Dependency / CI CVE gates (pip-audit, npm-audit, Trivy) | `.github/workflows/`, `docs/phase10-ci-security.md`, `docs/dependency-security-audit.md` |
| Secret hygiene (`.env`, certs, backups ignored; log redaction) | README, `docs/phase10-*` reports |
| TLS / proxy / rate limiting | `docs/phase10-staging-tls.md`, `docs/phase10-security-hardening.md` |

## Scope boundaries (intentional, not vulnerabilities)

- **Real-CA TLS and HSTS** are not configured yet — they are blocked on a production
  domain and documented as such, not as defects.
- **Unauthenticated `/metrics`** and **Grafana dev credentials/anonymous viewer** are
  explicit dev-stack defaults that operators must override before any wider deployment.
- **Vendored npm-CLI findings (F-05)** and **PostCSS build-time advisories (F-02)**
  remain documented exceptions with rationale in `.trivyignore` and
  `docs/phase10-remaining-dependency-findings.md`.