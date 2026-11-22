#!/usr/bin/env bash
# 用户态端到端验证：独立测试库，不影响演示库 museum。
set -euo pipefail
source /tmp/runtime.env

TEST_DB=museum_test
TEST_DATABASE_URL="postgresql://museum@/${TEST_DB}?host=/tmp&port=17760"
export DATABASE_URL="$TEST_DATABASE_URL"
export REDIS_URL='redis://127.0.0.1:16379/1'
export JWT_SECRET='test-secret'
export SEED_DEMO='false'

dropdb --if-exists -p 17760 "$TEST_DB"
createdb -p 17760 "$TEST_DB"

redis-cli -p 16379 -n 1 flushdb >/dev/null

cd /workspace/backend
/tmp/venv/bin/python -m scripts.seed_demo >/tmp/test-seed.log

/tmp/venv/bin/python -m src.worker >/tmp/test-worker.log 2>&1 &
WORKER_PID=$!
/tmp/venv/bin/python -m uvicorn src.app:app --host 127.0.0.1 --port 18000 >/tmp/test-api.log 2>&1 &
API_PID=$!
trap 'kill $WORKER_PID $API_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  curl -sf http://127.0.0.1:18000/health >/dev/null && break
  sleep 0.5
done

TEST_DATABASE_URL="$TEST_DATABASE_URL" /tmp/venv/bin/python tests/e2e_governance.py
