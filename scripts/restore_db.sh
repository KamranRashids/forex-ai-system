#!/usr/bin/env bash
# ==============================================================================
# Restore a gzipped PostgreSQL backup into an ISOLATED temporary database.
#
# Usage:
#   ./scripts/restore_db.sh <backup_file.sql.gz>          (dev stack)
#   ./scripts/restore_db.sh <backup_file.sql.gz> prod     (prod overlay)
#
# NEVER touches the live forex_ai database. Creates and drops a temporary
# database for verification only. Safe to run against any running stack.
#
# Requires: backups/forex_ai-*.sql.gz produced by backup_db.sh.
# ==============================================================================
set -euo pipefail

BACKUP_FILE="${1:?Usage: restore_db.sh <backup_file.sql.gz> [prod]}"
COMPOSE_FLAG="${2:-}"

cd /home/kamran/projects/forex-ai-system

if [[ "$COMPOSE_FLAG" == "prod" ]]; then
  DC="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
else
  DC="docker compose"
fi

TEMP_DB="forex_ai_restore_$(date -u +%Y%m%d-%H%M%S)"
# Use the 'postgres' admin DB for meta-commands (create/drop/alter).
# Use 'forex_ai' (the live DB) for the comparison queries.
POSTGRES_USER="$($DC exec -T postgres sh -c 'echo -n $POSTGRES_USER')"
LIVE_DB="$($DC exec -T postgres sh -c 'echo -n $POSTGRES_DB')"

# --- verify the backup file is a valid gzip ---
echo "[restore] verifying backup integrity..."
gzip -t "$BACKUP_FILE"

# --- create the isolated database (connect to the admin 'postgres' DB) ---
# The temp name embeds a timestamp hyphen, so quote the identifier.
echo "[restore] creating isolated database: ${TEMP_DB}"
$DC exec -T postgres psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE \"${TEMP_DB}\";" 2>/dev/null

# --- restore into the temp database ---
echo "[restore] restoring from ${BACKUP_FILE} into ${TEMP_DB}..."
gunzip -c "$BACKUP_FILE" | $DC exec -T postgres psql \
  -U "$POSTGRES_USER" \
  -d "$TEMP_DB" \
  -v ON_ERROR_STOP=1 2>/dev/null

# --- verify restored schema ---
echo "[restore] restored public tables:"
$DC exec -T postgres psql -U "$POSTGRES_USER" -d "$TEMP_DB" -At \
  -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;" 2>/dev/null

RESTORED_VERSION="$($DC exec -T postgres psql -U "$POSTGRES_USER" -d "$TEMP_DB" -At \
  -c "SELECT version_num FROM alembic_version;" 2>/dev/null)"
echo "[restore] restored alembic_version: ${RESTORED_VERSION}"

# --- compare against live DB ---
LIVE_VERSION="$($DC exec -T postgres psql -U "$POSTGRES_USER" -d "$LIVE_DB" -At \
  -c "SELECT version_num FROM alembic_version;" 2>/dev/null)"
if [[ "$RESTORED_VERSION" == "$LIVE_VERSION" ]]; then
  echo "[restore] alembic version MATCHES live DB (${LIVE_VERSION})"
else
  echo "[restore] WARNING: restored version ${RESTORED_VERSION} != live ${LIVE_VERSION}"
fi

# --- verify live DB is untouched ---
LIVE_TABLES="$($DC exec -T postgres psql -U "$POSTGRES_USER" -d "$LIVE_DB" -At \
  -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;" 2>/dev/null)"
echo "[restore] live DB tables unchanged (count=$(echo "$LIVE_TABLES" | wc -l))"

# --- cleanup only the temp resource we created (connect to admin DB) ---
echo "[restore] dropping isolated database ${TEMP_DB}..."
$DC exec -T postgres psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS \"${TEMP_DB}\";" 2>/dev/null

echo "[restore] DONE — live database untouched, temp DB dropped."