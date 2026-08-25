#!/usr/bin/env bash
# Phase 3 smoke: persisted versioned signals per closed bar, sub-second M5 latency.
set -e
cd /home/kamran/projects/forex-ai-system

# Both workers must share the universe so agents consume every timeframe ingested.
export MARKET_DATA_TIMEFRAMES=M5,M15,H1,H4,D1

echo "== starting api + ingest + agents workers =="
docker compose up -d --build api worker-ingest worker-agents 2>&1 | tail -3
sleep 30

PSQL="docker compose exec -T postgres psql -U forex -d forex_ai -Atc"
ANSI='s/\x1b\[[0-9;]*m//g'

echo "== signals per agent/symbol/timeframe =="
$PSQL "SELECT agent_id, symbol, timeframe, count(*), max(agent_version) FROM agent_signals GROUP BY 1,2,3 ORDER BY 2,3,1;" | head -24

echo "== pairs covered (need >= 3) =="
$PSQL "SELECT count(DISTINCT symbol) FROM agent_signals;"

echo "== M5 signals present =="
$PSQL "SELECT count(*) FROM agent_signals WHERE timeframe='M5';"

echo "== one row per closed bar per agent (no dupes) =="
$PSQL "SELECT 'dups=' || count(*) FROM (SELECT agent_id, symbol, timeframe, bucket_ts, count(*) FROM agent_signals GROUP BY 1,2,3,4 HAVING count(*)>1) d;"

echo "== latency: top total_ms from agents worker (strip ANSI) =="
docker compose logs worker-agents 2>/dev/null \
  | sed "$ANSI" | grep -oE 'total_ms=[0-9.]+' | cut -d= -f2 | sort -rn | head -3

echo "== SAFE MODE banner in agents worker =="
docker compose logs worker-agents 2>/dev/null | grep -c "SAFE MODE ACTIVE"

echo "== signals.stream depth =="
docker compose exec -T redis redis-cli XLEN signals.stream

echo "SMOKE COMPLETE"
