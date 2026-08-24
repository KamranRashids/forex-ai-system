#!/usr/bin/env bash
set -e
BASE=http://localhost:8000

echo "--- health/live ---"
curl -s $BASE/health/live; echo

echo "--- health/ready ---"
curl -s $BASE/health/ready; echo

echo "--- register first user (bootstrap admin) ---"
curl -s -X POST $BASE/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"smoke@example.com","password":"smoke-test-password-123"}'; echo

echo "--- login ---"
LOGIN=$(curl -s -X POST $BASE/api/v1/auth/login \
  -d 'username=smoke@example.com&password=smoke-test-password-123')
echo "$LOGIN" | head -c 200; echo

TOKEN=$(echo "$LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

echo "--- me ---"
curl -s $BASE/api/v1/auth/me -H "Authorization: Bearer $TOKEN"; echo

echo "--- refresh rotation (cookie jar) ---"
JAR=$(mktemp)
curl -s -c "$JAR" -X POST $BASE/api/v1/auth/login \
  -d 'username=smoke@example.com&password=smoke-test-password-123' > /dev/null
curl -s -b "$JAR" -c "$JAR" -X POST $BASE/api/v1/auth/refresh | head -c 120; echo
NEW_TOKEN=$(curl -s -b "$JAR" -c "$JAR" -X POST $BASE/api/v1/auth/refresh | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

echo "--- me with rotated token ---"
curl -s $BASE/api/v1/auth/me -H "Authorization: Bearer $NEW_TOKEN"; echo

echo "--- rate limit probe (6 rapid logins, wrong password) ---"
for i in 1 2 3 4 5 6; do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/v1/auth/login \
    -d 'username=smoke@example.com&password=wrong-password-xxxxx')
  printf "%s " "$CODE"
done; echo

echo "--- system/status (component matrix) ---"
curl -s $BASE/system/status | python3 -m json.tool | head -30

echo "--- SAFE MODE banner in api logs ---"
docker compose logs api 2>/dev/null | grep -m2 "SAFE MODE ACTIVE" || true
