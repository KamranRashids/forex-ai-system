#!/usr/bin/env bash
# Phase 2 smoke: >=3 pairs across M5..D1, restart survival, no gaps/duplicates.
set -e
cd /home/kamran/projects/forex-ai-system

export MARKET_DATA_TIMEFRAMES=M5,M15,H1,H4,D1
export MARKET_DATA_SYMBOLS=EURUSD,GBPUSD,USDJPY,AUDUSD

echo "== building/starting stack (api + worker-ingest) =="
docker compose build api >/dev/null 2>&1 || docker compose build api | tail -2
docker compose up -d postgres redis api worker-ingest 2>&1 | tail -4
sleep 20
docker compose ps --format '{{.Name}} {{.Status}}'

PSQL="docker compose exec -T postgres psql -U forex -d forex_ai -Atc"

echo "== coverage matrix (pairs x timeframes with stored bars) =="
$PSQL "SELECT i.symbol, c.timeframe, count(*), max(c.ts) FROM candles c JOIN instruments i ON i.id=c.instrument_id GROUP BY 1,2 ORDER BY 1,2;"

echo "== restart survival check =="
BEFORE=$($PSQL "SELECT coalesce(sum(n),0) FROM (SELECT count(*) n FROM candles GROUP BY instrument_id, timeframe, ts) t;")
MAX_BEFORE=$($PSQL "SELECT coalesce(max(ts)::text,'none') FROM candles WHERE timeframe='M15' AND instrument_id=(SELECT id FROM instruments WHERE symbol='EURUSD');")
echo "bars_before=$BEFORE max_m15_eurusd_before=$MAX_BEFORE"

docker compose restart worker-ingest >/dev/null
sleep 25

AFTER=$($PSQL "SELECT coalesce(sum(n),0) FROM (SELECT count(*) n FROM candles GROUP BY instrument_id, timeframe, ts) t;")
MAX_AFTER=$($PSQL "SELECT coalesce(max(ts)::text,'none') FROM candles WHERE timeframe='M15' AND instrument_id=(SELECT id FROM instruments WHERE symbol='EURUSD');")
echo "bars_after=$AFTER max_m15_eurusd_after=$MAX_AFTER"
if [ "$AFTER" -lt "$BEFORE" ]; then echo "FAIL: bar count dropped after restart"; exit 1; fi

echo "== gap check for EURUSD M15 (expected buckets vs stored since first bar) =="
$PSQL "
WITH bounds AS (
  SELECT min(ts) s, max(ts) e FROM candles
  WHERE timeframe='M15' AND instrument_id=(SELECT id FROM instruments WHERE symbol='EURUSD')
),
expected AS (
  SELECT generate_series((SELECT s FROM bounds), (SELECT e FROM bounds), interval '15 minutes') ts
)
SELECT 'missing=' || count(*) FROM expected e
LEFT JOIN candles c ON c.timeframe='M15' AND c.ts=e.ts
AND c.instrument_id=(SELECT id FROM instruments WHERE symbol='EURUSD')
WHERE c.ts IS NULL;"

echo "== duplicates check =="
$PSQL "SELECT 'dups=' || count(*) FROM (SELECT instrument_id, timeframe, ts, count(*) FROM candles GROUP BY 1,2,3 HAVING count(*)>1) d;"

echo "== breaker / staleness log signals (last 40 lines) =="
docker compose logs --tail=40 worker-ingest 2>/dev/null | grep -E 'ingest_cycle|staleness|breaker' | tail -5 || true

echo "== SAFE MODE banner present in worker logs =="
docker compose logs worker-ingest 2>/dev/null | grep -c "SAFE MODE ACTIVE"

echo "SMOKE COMPLETE"
