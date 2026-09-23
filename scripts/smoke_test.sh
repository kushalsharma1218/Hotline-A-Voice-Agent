#!/usr/bin/env bash
# Hotline guardrail smoke test (PRD §1 success metrics, §6.7, §8) against a RUNNING stack.
#
# Needs: docker compose stack up (postgres, n8n, ingress), workflows imported + published,
# n8n credentials "Hotline Postgres" and "Hotline Ingress Token (header auth)" linked.
# No external accounts: every call used here stops before Claude/Salesforce/Slack.
#
# Usage (from anywhere):   bash scripts/smoke_test.sh
# Overrides (env):          INGRESS_URL (default http://localhost:8000)
#                           N8N_URL     (default http://localhost:${N8N_HOST_PORT:-5678})
#                           PY          (python launcher; default: first of py -3, python3, python)
#                           POLL_S      (async poll timeout, default 20)
# Exit code: 0 if every check passes, 1 otherwise.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

envval() {  # envval KEY [default] -> value from environment, else .env, else default
  local key="$1" def="${2:-}" v=""
  v="${!key:-}"
  if [ -z "$v" ] && [ -f .env ]; then
    v="$(grep -E "^[[:space:]]*${key}=" .env | tail -n 1 | cut -d= -f2- | tr -d '\r')"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  fi
  printf '%s' "${v:-$def}"
}

PG_USER="$(envval POSTGRES_USER hotline)"
PG_DB="$(envval POSTGRES_DB hotline)"
INGRESS_URL="${INGRESS_URL:-http://localhost:8000}"
N8N_URL="${N8N_URL:-http://localhost:$(envval N8N_HOST_PORT 5678)}"
POLL_S="${POLL_S:-20}"
AGENT_ID="$(envval ELEVENLABS_AGENT_ID | cut -d, -f1)"
if [ -z "${PY:-}" ]; then
  for cand in "py -3" python3 python; do
    if $cand -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
      PY="$cand"; break
    fi
  done
fi
[ -n "${PY:-}" ] || { echo "no Python >= 3.8 found (set PY=...)"; exit 1; }

SEND="$PY scripts/send_test_webhook.py --url $INGRESS_URL/webhooks/elevenlabs"
RUN_ID="$(date +%s)$$"
PASS=0; FAIL=0; RESULTS=()

sql() {  # sql "<query>" -> unaligned, tuples-only output
  MSYS_NO_PATHCONV=1 docker compose exec -T postgres \
    psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>&1 | tr -d '\r'
}

poll_sql() {  # poll_sql "<query>" "<expected>" -> echoes last value; 0 when it matched
  local q="$1" want="$2" got="" deadline=$((SECONDS + POLL_S))
  while :; do
    got="$(sql "$q")"
    [ "$got" = "$want" ] && { echo "$got"; return 0; }
    [ "$SECONDS" -ge "$deadline" ] && { echo "$got"; return 1; }
    sleep 1
  done
}

check() {  # check "<name>" <0|1 ok> "<actual>"
  local name="$1" ok="$2" actual="$3"
  if [ "$ok" = "0" ]; then
    PASS=$((PASS + 1)); RESULTS+=("PASS | $name | $actual")
    printf 'PASS  %-58s %s\n' "$name" "$actual"
  else
    FAIL=$((FAIL + 1)); RESULTS+=("FAIL | $name | $actual")
    printf 'FAIL  %-58s %s\n' "$name" "$actual"
  fi
}

http_code() {  # http_code <curl args...> -> status code only
  curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@"
}

rows_for() { sql "SELECT count(*) FROM hotline.call_events WHERE conversation_id='$1'"; }

status_codes() {  # extract "<code>" from each "send i/n: <code> <body>" line
  grep -E '^send [0-9]+/[0-9]+:' | awk '{print $3}' | paste -sd, -
}

echo "== Hotline smoke test: ingress=$INGRESS_URL n8n=$N8N_URL python='$PY'"

# --- 0. preflight ---------------------------------------------------------------------
code="$(http_code "$INGRESS_URL/healthz")"
[ "$code" = "200" ]; check "preflight: ingress /healthz 200" $? "http=$code"
code="$(http_code "$N8N_URL/healthz")"
[ "$code" = "200" ]; check "preflight: n8n /healthz 200" $? "http=$code"
out="$(sql 'SELECT 1')"
[ "$out" = "1" ]; check "preflight: psql via docker compose exec" $? "$out"

# --- 1. forged signature (wrong secret) -> 401, no row ----------------------------------
cid="conv_smoke_forged_$RUN_ID"
codes="$($SEND --conversation-id "$cid" --short --secret "not-the-real-secret" | status_codes)"
rows="$(rows_for "$cid")"
[ "$codes" = "401" ] && [ "$rows" = "0" ]
check "forged signature (wrong secret) -> 401, no row" $? "http=$codes rows=$rows"

# --- 2. tampered body (valid sig, byte flipped after signing) -> 401, no row -------------
cid="conv_smoke_tamper_$RUN_ID"
codes="$($SEND --conversation-id "$cid" --short --tamper | status_codes)"
rows="$(rows_for "$cid")"
[ "$codes" = "401" ] && [ "$rows" = "0" ]
check "tampered body -> 401, no row" $? "http=$codes rows=$rows"

# --- 3. stale timestamp (2 h) -> 401, no row -------------------------------------------
cid="conv_smoke_stale_$RUN_ID"
codes="$($SEND --conversation-id "$cid" --short --stale | status_codes)"
rows="$(rows_for "$cid")"
[ "$codes" = "401" ] && [ "$rows" = "0" ]
check "stale timestamp (2h) -> 401, no row" $? "http=$codes rows=$rows"

# --- 4. agent not in allowlist -> 200 ignored, no row ----------------------------------
cid="conv_smoke_agent_$RUN_ID"
out="$($SEND --conversation-id "$cid" --short --agent-id "agent_not_allowed_$RUN_ID")"
codes="$(printf '%s\n' "$out" | status_codes)"
rows="$(rows_for "$cid")"
[ "$codes" = "200" ] && printf '%s' "$out" | grep -q '"ignored"' && [ "$rows" = "0" ]
check "unknown agent_id -> 200 ignored, no row" $? "http=$codes rows=$rows"

# --- 5. duplicate delivery x3 -> one row, one forward, one n8n execution ---------------
cid="conv_smoke_dup_$RUN_ID"
out="$($SEND --conversation-id "$cid" --short --repeat 3)"
bodies="$(printf '%s\n' "$out" | grep -oE '"status":"[a-z_]+"' | cut -d'"' -f4 | paste -sd, -)"
rows="$(rows_for "$cid")"
[ "$bodies" = "accepted,duplicate,duplicate" ] && [ "$rows" = "1" ]
check "duplicate x3 -> accepted,duplicate,duplicate; 1 row" $? "responses=$bodies rows=$rows"
status="$(poll_sql "SELECT status FROM hotline.call_events WHERE conversation_id='$cid'" LOGGED_ONLY)"
sleep 2  # let any (wrong) extra forwards land before counting
fa="$(sql "SELECT forward_attempts FROM hotline.call_events WHERE conversation_id='$cid'")"
[ "$fa" = "1" ]
check "duplicate x3 -> forward_attempts == 1" $? "forward_attempts=$fa status=$status"
execs="$(sql "SELECT count(*) FROM public.execution_entity e JOIN public.execution_data d ON d.\"executionId\" = e.id WHERE e.\"workflowId\" = 'HtlnPostCall0000' AND d.data LIKE '%$cid%'")"
[ "$execs" = "1" ]
check "duplicate x3 -> exactly 1 n8n execution" $? "executions=$execs"

# --- 6. direct call to n8n webhook bypassing ingress -> 403 ----------------------------
code="$(http_code -X POST -H 'Content-Type: application/json' -d '{"conversation_id":"x"}' \
  "$N8N_URL/webhook/hotline/post-call")"
[ "$code" = "403" ]; check "n8n webhook without X-Hotline-Token -> 403" $? "http=$code"
code="$(http_code -X POST -H 'Content-Type: application/json' -H 'X-Hotline-Token: wrong' \
  -d '{"conversation_id":"x"}' "$N8N_URL/webhook/hotline/post-call")"
[ "$code" = "403" ]; check "n8n webhook with wrong X-Hotline-Token -> 403" $? "http=$code"

# --- 7. too-short transcript -> LOGGED_ONLY, decisions row, no Claude call -------------
cid="conv_smoke_short_$RUN_ID"
codes="$($SEND --conversation-id "$cid" --short | status_codes)"
status="$(poll_sql "SELECT status FROM hotline.call_events WHERE conversation_id='$cid'" LOGGED_ONLY)"
ok=$?
ut="$(sql "SELECT user_turns FROM hotline.call_events WHERE conversation_id='$cid'")"
[ "$codes" = "200" ] && [ "$ok" = "0" ]
check "too-short (1 caller turn) -> call_events LOGGED_ONLY" $? "http=$codes status=$status user_turns=$ut"
dec="$(sql "SELECT route || '/' || (decided_at IS NOT NULL) FROM hotline.decisions WHERE conversation_id='$cid'")"
[ "$dec" = "LOGGED_ONLY/true" ]
check "too-short -> decisions route LOGGED_ONLY" $? "decision=${dec:-<none>}"
runs="$(sql "SELECT count(*) FROM hotline.extraction_runs WHERE conversation_id='$cid'")"
[ "$runs" = "0" ]
check "too-short -> 0 extraction_runs (no Claude call)" $? "extraction_runs=$runs"
SHORT_CID="$cid"

# --- 8. admin replay auth -------------------------------------------------------------
code="$(http_code -X POST -H 'Authorization: Bearer wrong-token' "$INGRESS_URL/admin/replay/$SHORT_CID")"
[ "$code" = "401" ]; check "admin replay with wrong token -> 401" $? "http=$code"
code="$(http_code -X POST "$INGRESS_URL/admin/replay/$SHORT_CID")"
[ "$code" = "401" ]; check "admin replay with no token -> 401" $? "http=$code"
ADMIN_TOKEN_VAL="$(envval ADMIN_TOKEN)"
if [ -n "$ADMIN_TOKEN_VAL" ]; then
  code="$(http_code -X POST -H "Authorization: Bearer $ADMIN_TOKEN_VAL" "$INGRESS_URL/admin/replay/$SHORT_CID")"
  [ "$code" = "409" ]
  check "admin replay (valid token) of LOGGED_ONLY call -> 409" $? "http=$code"
fi

echo
echo "== $PASS passed, $FAIL failed"
[ "$FAIL" = "0" ]
