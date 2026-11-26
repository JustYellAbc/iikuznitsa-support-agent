#!/usr/bin/env bash
# Сквозная проверка поднятого стенда по T04: /health, диалог покупателя, ход агента (один llm_call),
# обращение к оператору, вход менеджера и телеметрия.
# Использование: BASE_URL=https://<ip>.sslip.io OPERATOR_PASSWORD=... ./scripts/smoke_test.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost}"
if [[ -z "${OPERATOR_PASSWORD:-}" && -f .env ]]; then
  OPERATOR_USER="${OPERATOR_USER:-$(grep -E '^OPERATOR_USER=' .env | cut -d= -f2- || true)}"
  OPERATOR_PASSWORD="$(grep -E '^OPERATOR_PASSWORD=' .env | cut -d= -f2- || true)"
fi
: "${OPERATOR_PASSWORD:?Нужен OPERATOR_PASSWORD (env или .env)}"
OPERATOR_USER="${OPERATOR_USER:-manager}"

JARS="$(mktemp -d)"
trap 'rm -rf "$JARS"' EXIT
CUSTOMER="$JARS/customer" MANAGER="$JARS/manager"
API="$BASE_URL/api/v1"

json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
key() { python3 -c "import uuid; print('smoke-' + uuid.uuid4().hex)"; }
post() {  # post <jar> <path> <json>
  curl -fsS -b "$1" -c "$1" -X POST "$API$2" -H 'Content-Type: application/json' -H "Idempotency-Key: $(key)" -d "$3"
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "1. GET /health"
curl -fsS "$BASE_URL/health"; echo

echo "2. Покупатель открывает диалог (cookie support_session)"
CID=$(post "$CUSTOMER" /conversations '' | json 'd["conversationId"]')
echo "   conversationId=$CID"

echo "3. Реплика покупателя -> ход агента"
post "$CUSTOMER" "/conversations/$CID/actions" \
  '{"type":"message","text":"Смоук-тест: сколько стоит MDaemon на 20 пользователей?"}' >/dev/null

echo "4. Опрос журнала до ответа агента"
for _ in $(seq 1 60); do
  PAGE=$(curl -fsS -b "$CUSTOMER" "$API/conversations/$CID/events?after=0")
  if [[ $(echo "$PAGE" | json 'any(e["data"].get("author") == "agent" for e in d["events"])') == "True" ]]; then
    echo "$PAGE" | json '"\n".join("   #%s %s/%s: %s" % (e["seq"], e["data"].get("author"), e["data"].get("purpose"),
                                                           e["data"].get("text", "")[:110]) for e in d["events"])'
    break
  fi
  sleep 1
done

echo "5. Позвать оператора"
post "$CUSTOMER" "/conversations/$CID/actions" '{"type":"request_operator"}' >/dev/null

echo "6. Роли: телеметрия без сессии -> 401, с сессией покупателя -> 403"
[[ $(code "$API/manager/telemetry") == "401" ]] || { echo "   ожидали 401"; exit 1; }
[[ $(code -b "$CUSTOMER" "$API/manager/telemetry") == "403" ]] || { echo "   ожидали 403"; exit 1; }

echo "7. Вход менеджера, очередь, трейс и телеметрия"
curl -fsS -c "$MANAGER" -X POST "$API/auth/manager/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$OPERATOR_USER\",\"password\":\"$OPERATOR_PASSWORD\"}" >/dev/null
curl -fsS -b "$MANAGER" "$API/manager/handoffs" | json '"   открытых обращений: %d" % len(d["items"])'
curl -fsS -b "$MANAGER" "$API/manager/conversations/$CID/trace" | json '"   ходов агента в диалоге: %d" % len(d)'
curl -fsS -b "$MANAGER" "$API/manager/telemetry" | json '"   today_spend_rub=%s budget_left_rub=%s\n   steps=%s" % (
    d["today_spend_rub"], d["budget_left_rub"], d["steps"])'

echo "OK"
