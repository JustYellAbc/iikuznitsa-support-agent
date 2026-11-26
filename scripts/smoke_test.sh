#!/usr/bin/env bash
# Сквозная проверка поднятого стенда: /health, сессия, ход агента (один llm_call), телеметрия.
# Использование: BASE_URL=https://176-53-253-178.sslip.io OPERATOR_PASSWORD=... ./scripts/smoke_test.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost}"
if [[ -z "${OPERATOR_PASSWORD:-}" && -f .env ]]; then
  OPERATOR_USER="${OPERATOR_USER:-$(grep -E '^OPERATOR_USER=' .env | cut -d= -f2- || true)}"
  OPERATOR_PASSWORD="$(grep -E '^OPERATOR_PASSWORD=' .env | cut -d= -f2- || true)"
fi
AUTH="${OPERATOR_USER:-manager}:${OPERATOR_PASSWORD:?Нужен OPERATOR_PASSWORD (env или .env)}"

json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

echo "1. GET /health"
curl -fsS "$BASE_URL/health"; echo

echo "2. POST /api/sessions"
SID=$(curl -fsS -X POST "$BASE_URL/api/sessions" -H 'Content-Type: application/json' \
  -d '{"client_context":{"source":"smoke_test"}}' | json 'd["id"]')
echo "   session=$SID"

echo "3. POST /api/sessions/$SID/messages"
curl -fsS -X POST "$BASE_URL/api/sessions/$SID/messages" -H 'Content-Type: application/json' \
  -d '{"text":"Смоук-тест: сколько стоит MDaemon на 20 пользователей?"}' >/dev/null

echo "4. Ждём ответ агента"
for _ in $(seq 1 60); do
  STATE=$(curl -fsS "$BASE_URL/api/sessions/$SID")
  if [[ $(echo "$STATE" | json 'd["busy"] is False and d["messages"][-1]["role"] != "client"') == "True" ]]; then
    echo "$STATE" | json '"   " + d["messages"][-1]["role"] + ": " + d["messages"][-1]["text"][:160]'
    echo "$STATE" | json '"   decision=%s cost_rub=%s" % (d["turns"][-1]["decision"], d["turns"][-1]["total_cost_rub"])'
    break
  fi
  sleep 1
done

echo "5. Basic Auth закрывает телеметрию"
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/api/telemetry/summary")
[[ "$CODE" == "401" ]] || { echo "   ожидали 401, получили $CODE"; exit 1; }

echo "6. GET /api/telemetry/summary"
curl -fsS -u "$AUTH" "$BASE_URL/api/telemetry/summary" | json '"   today_spend_rub=%s budget_left_rub=%s\n   steps=%s\n   last=%s" % (d["today_spend_rub"], d["budget_left_rub"], d["steps"], d["recent_calls"][:1])'

echo "OK"
