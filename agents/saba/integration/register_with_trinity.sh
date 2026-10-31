#!/usr/bin/env bash
# Registers the Academy cycle schedule on the live Saba through Trinity's own API, and can trigger one cycle immediately.
# Needs a Trinity API token (admin JWT or an MCP key with access to the agent). The script never reads or stores credentials:
#   export TRINITY_TOKEN=...            # e.g. from POST /api/token or the Trinity UI
#   integration/register_with_trinity.sh [--run-now]
# Env: TRINITY_URL (default http://localhost:8000), AGENT (default saba-pulsit-online), CRON (default "0 */6 * * *" = 4 cycles/day).
set -euo pipefail
: "${TRINITY_TOKEN:?set TRINITY_TOKEN}"
URL="${TRINITY_URL:-http://localhost:8000}"; AGENT="${AGENT:-saba-pulsit-online}"; CRON="${CRON:-0 */6 * * *}"
H=(-H "Authorization: Bearer $TRINITY_TOKEN" -H "Content-Type: application/json")
echo "agent: $(curl -fsS "${H[@]}" "$URL/api/agents/$AGENT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("name"), d.get("status"))')"
existing=$(curl -fsS "${H[@]}" "$URL/api/agents/$AGENT/schedules" | python3 -c 'import json,sys; print(" ".join(s["id"] for s in json.load(sys.stdin) if s["name"]=="Academy research cycle"))')
if [ -n "$existing" ]; then echo "schedule already exists ($existing); not creating a second one"; else
  curl -fsS "${H[@]}" -X POST "$URL/api/agents/$AGENT/schedules" -d "$(python3 - <<PY
import json,os
print(json.dumps({"name":"Academy research cycle","cron_expression":os.environ.get("CRON","0 */6 * * *"),"message":"/academy-cycle",
 "timezone":"UTC","enabled":True,"description":"Autonomous Academy research cycle (max 4/day; budget enforced by the bridge)",
 "max_retries":0}))
PY
)" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("created schedule", d.get("id"), d.get("cron_expression"), "next", d.get("next_run_at"))'
fi
if [ "${1:-}" = "--run-now" ]; then
  echo "triggering one cycle now ..."
  curl -fsS "${H[@]}" -X POST "$URL/api/agents/$AGENT/task" -d '{"message":"/academy-cycle","async_mode":true}' | head -c 1500; echo
  echo "started asynchronously: follow it in the Trinity UI (agent > executions) or GET $URL/api/agents/$AGENT/executions"
fi
