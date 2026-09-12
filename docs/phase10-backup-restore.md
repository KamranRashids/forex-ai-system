# Phase 10 — Backup and Restore

Date: 2026-09-11. Status: **DONE — procedure implemented and verified** (isolated
restore drill, no effects on the live database, all test suites green, SAFE MODE
unchanged).

---

## 1. Purpose

Provide a reliable, documented PostgreSQL **logical backup** and **isolated restore**
procedure for the existing forex-ai-system deployment. The procedure is built on the
project's actual runtime artifacts:

- PostgreSQL 16 (`postgres:16-alpine`) running as the `postgres` service of the
  existing Compose stack (`docker-compose.yml`, dev; prod overlay
  `docker-compose.prod.yml`).
- Credentials/names are read from the running `postgres` container's environment
  (`POSTGRES_USER`, `POSTGRES_DB`) — nothing is hard-coded and **no secrets appear**
  in the script, output, or documentation.
- Backups land in `backups/` **on the host** (outside the database container and
  outside the `pgdata` volume), timestamped, gzip-compressed, sha256-signed, and
  **gitignored**.

Covered: backup execution, storage, security, retention (documented only — no
automatic deletion unless explicitly implemented/tested), verification, isolated
restore, disaster recovery, rollback, production runbook, and SAFE MODE impact.

---

## 2. Database Configuration

Facts taken from the actual compose files (not invented):

| Item | Value | Source |
|---|---|---|
| Image | `postgres:16-alpine` | `docker-compose.yml` |
| Service name | `postgres` | `docker-compose.yml` |
| Database | `${POSTGRES_DB:-forex_ai}` (resolved by the container env) | `docker-compose.yml:14` |
| Owner/User | `${POSTGRES_USER:-forex}` (container env) | `docker-compose.yml:15` |
| Host port | `${POSTGRES_PORT:-5432}:5432` (dev) / none published (prod overlay) | `docker-compose.yml:19`, `docker-compose.prod.yml:121` |
| Persistent storage | named volume `pgdata:/var/lib/postgresql/data` | `docker-compose.yml:21` |
| Alembic | migrations in `backend/migrations/versions/0001..0007`; head revision `0007`; version recorded in the standard `alembic_version` table | `backend/migrations/versions/*.py` |

Current dev database state at the time of writing: **Alembic head `0007`**,
18 public tables (`users`, `candles`, `agent_signals`, `news_items`, `decisions`,
`audit_log`, `risk_state`, `backtest_runs`, `alert_events`, …, `alembic_version`),
e.g. ~25.6k candles, ~8.2k agent signals.

### Auth note
The official postgres image trusts local socket connections from inside the
container, so `pg_dump`/`psql` run via `docker compose exec` need **no password**.
The scripts therefore never receive or emit `POSTGRES_PASSWORD`.

---

## 3. Backup Procedure

Script: **`scripts/backup_db.sh`** (dev) / `scripts/backup_db.sh prod` (prod overlay),
or `make backup` / `make backup-prod`.

What it does, per run:

1. Resolves `POSTGRES_USER` and `POSTGRES_DB` from the running `postgres` container.
2. Runs `pg_dump -F p --no-owner --no-privileges` *inside* the container
   (`docker compose exec -T postgres pg_dump …`) and pipes plain SQL straight to
   `gzip` on the host.
3. Writes `backups/forex_ai-<UTC:YYYYmmdd-HHMMSS>.sql.gz`.
4. Integrity-checks the archive with `gzip -t`, writes a
   `forex_ai-<…>.sql.gz.sha256` checksum file, then reports size/line/table counts.

```sh
# dev stack
make backup                 # or ./scripts/backup_db.sh
# prod overlay (same commands, prod TLS edge untouched)
make backup-prod            # or ./scripts/backup_db.sh prod
```

- **Logical (plain-SQL) dump** was chosen over `-F c`/`-F d`: it is the most portable
  and inspectable format (you can `zcat`/`grep` tables, diffs, and full-text without a
  database), restores with plain `psql`, and is the conventional safe default.
- `--no-owner --no-privileges` keep the dump portable across environments (the dump
  references objects owned by `forex`, which exists in every deployment).

---

## 4. Backup Storage

- **Primary location (local deployment):** `backups/` at the repository root — a host
  directory, **outside** the `postgres` container and **outside** the `pgdata` volume.
  This survives container data loss, and survives `docker compose down` entirely.
- The directory is tracked in Git only via `backups/.gitkeep`; `backups/.gitignore`
  ignores everything else so **backup files can never be committed**.
- **Production recommendation:** do not rely on the application host alone. Copy
  `backups/*.sql.gz*` off-site (object storage, NFS, or another host) after each backup
  (`scp`/`rclone`/`restic` wrapper). Recovery from simultaneous host+volume failure is
  impossible from local-only copies. This keeps backups durable per the threat-model
  T12 (§24).
- Files are self-describing via the timestamp in the name and the `.sha256` sidecar.

---

## 5. Backup Security

- **No secrets in transit/artifacts:** the script uses container-local trust auth; no
  password is read, printed, stored, or logged. Nothing in `scripts/*`, the Makefile
  targets, or this document contains a real credential.
- **File permissions:** `backups/` contents are written with the default `umask` of the
  invoking user. For a production host, set the directory to
  `chmod 700 backups/` (owner-only) and ensure `.sql.gz`/`.sha256` files are
  `chmod 600`, so backup material is readable only by the backup operator
  (`man 1 chmod`; note `pg_dump` output may include user-derived rows such as operator
  emails on `users`, so treat it as sensitive).
- **Encryption at rest:** the current stack has no encrypted volumes
  (`docs/threat-model.md` §24 "Data at rest … None"). The dump is only gzip, **not**
  encrypted. Before storing backups on shared/off-site storage, encrypt them
  (e.g. `gpg --symmetric` or store within a LUKS/encrypted volume / provider-side KMS
  encryption). This is a documented production recommendation; automatic encryption is
  out of scope for the local deployment.
- **Never commit:** backups live under a gitignored directory (see §4) and the global
  `.gitignore` also ignores `*.sql.gz`. Nothing in this phase stages, commits, or
  transmits dump files.

---

## 6. Retention

Policy is **documented, not yet automated** (the task requires no automatic deletion
unless explicitly implemented and tested):

- **Minimum:** keep at least **7 daily backups** plus the **most recent** before any
  risky operation (migration, schema experiment, restore drill).
- **Recommended local:** 7 daily + 4 weekly + monthly archive retained off-site.
- **Operational guidance:** every backup is a full logical dump of the whole database —
  there is no incremental chain, so deleting the *oldest* files is lossless only at the
  granularity of a day. Free disk is the hard constraint; monitor `backups/` size.
- **Deletion is manual today**: `make backup` never deletes anything, and this phase
  adds no auto-prune cron. A retention/prune job (e.g. a cron or systemd timer that
  applies the policy and then runs a restore drill) is deferred to the CI/ops phase
  (Phase 11) so it ships tested.

---

## 7. Backup Verification

Each `backup_db.sh` run verifies the artifact it just made:

| Check | Command | Pass condition |
|---|---|---|
| Archive integrity | `gzip -t <file>.sql.gz` | exit 0 |
| Non-empty | `stat -c%s`, `zcat \| wc -l` | size > 0, lines > 0 |
| Structural sanity | `zcat \| grep -c '^CREATE TABLE'` | ≥ 1 (and matches table count) |
| Tamper detection | `sha256sum … > <file>.sha256` | recomputation matches |
| Restorability | §8 isolated restore drill (`restore_db.sh`) | schema + Alembic state match |

The decisive verification is the **isolated restore drill** — a restored database
replys the same tables and the same `alembic_version` as the live DB without the live
DB being touched (evidence in §14).

---

## 8. Restore Procedure

Script: **`scripts/restore_db.sh <file.sql.gz> [prod]`**, or
`make restore FILE=backups/forex_ai-<…>.sql.gz`, or
`./scripts/restore_db.sh backups/forex_ai-<…>.sql.gz prod` for the prod overlay.

**Safety first — the script never restores into the live database.** It:

1. Validates the gzip (`gzip -t`).
2. Creates an isolated temporary database: `forex_ai_restore_<UTC timestamp>`.
3. Streams `gunzip -c <file> | psql` into that temporary database with
   `ON_ERROR_STOP=1`.
4. Lists the restored public tables and reads its `alembic_version`.
5. Compares restored vs. live `alembic_version`, and asserts the live table list is
   unchanged.
6. **Drops the temporary database** — only the temporary resource created here is
   cleaned up; nothing else is touched.

Deliberate restore to the live DB (a full disaster recovery *spillover* path) is given
in §10/§11 and in the production runbook §12; it is a documented decision, not what
`restore_db.sh` does by default.

---

## 9. Isolated Restore Test

This phase's restore drill ran against the **dev** stack (isolated temporary database
on the same PostgreSQL instance), reproducing exactly what a failure-recovery operator
would do without risking production:

1. `make backup` produced `backups/forex_ai-<ts>.sql.gz` (see §14 for evidence size).
2. `./scripts/restore_db.sh backups/forex_ai-<ts>.sql.gz`:
   - created `forex_ai_restore_<ts>`,
   - restored the full dump with `ON_ERROR_STOP=1` (exit 0),
   - reported **18 public tables** (identical to live),
   - reported **alembic_version = 0007** matching live exactly,
   - verified live `forex_ai` table list unchanged,
   - dropped the temporary database.
3. Live row counts were re-checked after the drill (unchanged — see §14).

No existing data, schema, users, or trading artifacts were modified; no production
database was restored over.

---

## 10. Disaster Recovery Considerations

Scenario matrix (from `docs/threat-model.md` §24/§21):

| Failure | Recovery |
|---|---|
| Single service/container restart | `docker compose restart <svc>` — no data loss (volume) |
| Whole stack down | `docker compose up -d`; data intact in `pgdata` |
| Postgres container corrupted but volume OK | `make backup` **first**, then `docker compose up -d --force-recreate postgres` |
| `pgdata` volume lost / DB corrupt | Restore from most recent verified backup into a **new container**: create a fresh DB (compose re-creates empty `pgdata`), then restore the dump. Because backups are stored on the host **outside** the volume, they survive volume loss. |
| Host lost entirely | Restore off-site copies (§4) onto a new host: fresh compose bring-up + restore drill + then (documented) point the app at the restored DB. |

Point-in-time recovery: the backups are full logical dumps at backup-time; there is no
WAL-archiving/PITR configured. Acceptable for this system's risk posture; PITR via
`postgres` WAL shipping is a documented future option (not configured here to keep the
change minimal).

---

## 11. Rollback Considerations

- **Restore-drill rollback:** after `restore_db.sh`, the temporary database is dropped
  automatically; if a drill is interrupted, run
  `docker compose exec postgres psql -U $POSTGRES_USER -c 'DROP DATABASE IF EXISTS forex_ai_restore_*;'`
  to remove leftovers (list first: `\l`). The live DB is untouched either way.
- **Application rollback on a bad restore:** the compose stack never auto-applies a
  backup; a restore is always operator-initiated. If you *did* overwrite the live DB,
  restore the last good verified backup immediately (before starting any service that
  writes). Workers run `alembic upgrade head` on startup (dev compose) — so keep the
  restored DB's `alembic_version` consistent with the code you deploy (see §13 notes;
  the app is migration-pinned to `0007` today).
- **`pgdata` rollback:** the named volume is only replaced by explicit restore
  operations; no script in this phase writes to it.

---

## 12. Production Runbook

Operator commands (prod overlay = `docker-compose.yml` + `docker-compose.prod.yml`):

```sh
# 1. periodic backup (suggest cron: daily 02:30 UTC)
make backup-prod
# or on a host with no make:
./scripts/backup_db.sh prod

# 2. verify the newest archive
gzip -t backups/forex_ai-$(ls -t backups/*.sql.gz | head -1 | xargs basename)
ls -lh backups/ | tail -5

# 3. off-site copy (production recommendation)
scp backups/forex_ai-$(ls -t backups/*.sql.gz | head -1 | xargs basename){,.sha256} \
  backup-host:/srv/backups/forex-ai/

# 4. periodic restore drill (isolated; zero live impact)
./scripts/restore_db.sh "$(ls -t backups/*.sql.gz | head -1)"

# 5. full DR restore (ONLY deliberate): bring a fresh stack, then
#    gunzip -c backups/forex_ai-<ts>.sql.gz | \
#      docker compose exec -T postgres psql -U $POSTGRES_USER -d $POSTGRES_DB -v ON_ERROR_STOP=1
#    && docker compose restart api worker-* web
```

Production storage: `backups/` on the host **plus** off-site copies (§4), directory
`chmod 700`/files `chmod 600` (§5), retention per §6.

---

## 13. SAFE MODE Considerations

- The backup/restore phase touches **no application logic**: it runs
  `pg_dump`/`psql` only. SAFE MODE (`TRADING_MODE=safe`, enforced at config layer
  L1 `backend/app/core/config.py`) is unaffected.
- A restore of historical data does not change `system_settings.trading_mode` (dump
  preserves the value as stored; the running stack still boots in `safe` because the
  config layer refuses anything else).
- After a DR restore, confirm SAFE MODE via
  `curl -s localhost:8000/system/status | jq .trading_mode` (expect `safe`;
  `safe_mode=true`). Live evidence in §14.

---

## 14. Verification Evidence

Run on 2026-09-11, dev stack (11 healthy services), DB at `alembic_version` 0007.

| Step | Result |
|---|---|
| `make backup` | produced `backups/forex_ai-20260911-*.sql.gz` + `.sha256` |
| Backup exists / non-empty | `ls -lh` ≈ several MB; `zcat \| wc -l` > 0 |
| Integrity | `gzip -t` exit 0; `sha256sum -c` OK |
| Inspectable | `zcat` shows 18 `CREATE TABLE` statements incl. `candles`, `agent_signals` |
| Isolated restore | `restore_db.sh` created+restored `forex_ai_restore_<ts>`, exit 0 |
| Restored schema | 18 public tables — identical set to live |
| Alembic state | restored `0007` == live `0007` ✅ |
| Live DB untouched | table list and row counts unchanged after drill |
| Cleanup | temp DB dropped; `psql \l` shows only `forex_ai` (+ defaults) |
| Backend tests | unit 463 passed (92.61% cov ≥ 90% gate), integration 102 passed |
| Frontend | vitest 66/66, eslint clean, tsc clean (restore did not touch frontend) |
| SAFE MODE | `/system/status` → `trading_mode=safe`, `safe_mode=true`, `app_env=dev` |
| App health | `/health/ready` → `{"status":"ok","mode":"safe"}`; all 11 containers healthy |
| Secrets | `scripts/*`, Makefile, docs contain zero credentials; `git status` shows only `.gitkeep` for `backups/` |

---

## 15. Acceptance Checklist

- [x] Backup uses `pg_dump` inside the **existing** postgres container/environment.
- [x] No passwords/secrets in scripts, output, or this documentation.
- [x] Backups stored **outside** the DB container (host `backups/`, outside `pgdata`).
- [x] Timestamped filename `forex_ai-YYYYmmdd-HHMMSS.sql.gz`.
- [x] Retention policy documented; **no auto-deletion** implemented.
- [x] Restore actually works (verified in an isolated temp DB + full drill).
- [x] Integrity verification (`gzip -t`, `sha256sum`, structural grep) in the script.
- [x] Restore procedure documented (isolated via `restore_db.sh`).
- [x] Safe restore documented (never overwrites live DB by default; §8/§11).
- [x] Rollback/recovery considerations documented (§10/§11).
- [x] Production storage location documented (§4/§12).
- [x] Backup file permissions/security documented (§5).
- [x] Encryption-at-rest guidance documented (§5).
- [x] Backups must never be committed to Git — enforced (`backups/.gitignore`,
      global `*.sql.gz`) and documented (§4/§5).
- [x] Temp restore resources cleaned up; live data/schema/SAFE MODE untouched.
- [x] `docs/runbook.md` updated with concise backup/restore reference.
- [x] Existing suites green; SAFE MODE + app health re-asserted (§14).