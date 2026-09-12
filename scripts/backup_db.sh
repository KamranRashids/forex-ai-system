#!/usr/bin/env bash
# ==============================================================================
# PostgreSQL logical backup of the forex-ai database.
#
# Usage:
#   ./scripts/backup_db.sh          (dev stack — default)
#   ./scripts/backup_db.sh prod     (prod overlay with docker-compose.prod.yml)
#
# Produces: backups/forex_ai-YYYYmmdd-HHMMSS.sql.gz
#           backups/forex_ai-YYYYmmdd-HHMMSS.sql.gz.sha256
#
# The script reads POSTGRES_USER / POSTGRES_DB from the running postgres
# container environment — no secrets are embedded here or in the output.
# Backups must never be committed to Git (see backups/.gitignore).
# ==============================================================================
set -euo pipefail

COMPOSE_FILE="${1:-}"

cd /home/kamran/projects/forex-ai-system

# Build docker compose base command
if [[ "$COMPOSE_FILE" == "prod" ]]; then
  DC="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
else
  DC="docker compose"
fi

BACKUP_DIR="backups"
mkdir -p "$BACKUP_DIR"

TIMESTAMP="$(date -u +%Y%m%d-%H%M%S)"
FILE="${BACKUP_DIR}/forex_ai-${TIMESTAMP}.sql.gz"
CHECKSUM="${FILE}.sha256"

# Read user/db from the postgres container environment (no password needed;
# local socket connections use trust auth in the official postgres image).
POSTGRES_USER="$($DC exec -T postgres sh -c 'echo -n $POSTGRES_USER')"
POSTGRES_DB="$($DC exec -T postgres sh -c 'echo -n $POSTGRES_DB')"

echo "[backup] database=${POSTGRES_DB}  user=${POSTGRES_USER}"
echo "[backup] dumping → ${FILE}"

$DC exec -T postgres pg_dump \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  -F p \
  --no-owner \
  --no-privileges \
  2>/dev/null | gzip > "$FILE"

# Integrity verification
gzip -t "$FILE"
sha256sum "$FILE" > "$CHECKSUM"

SIZE=$(stat -c%s "$FILE")
LINES=$(zcat "$FILE" | wc -l)
TABLES=$(zcat "$FILE" | grep -c "^CREATE TABLE" || true)

echo "[backup] OK: ${SIZE} bytes, ${LINES} lines, ${TABLES} CREATE TABLE statements"
echo "[backup] sha256: $(cut -d' ' -f1 "$CHECKSUM")"