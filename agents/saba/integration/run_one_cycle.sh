#!/usr/bin/env bash
# Starts ONE /academy-cycle task on the existing Saba through Trinity, waits for it, and prints her actual trace.
# Does not create or enable any schedule. Needs a prior integration/trinity_login.sh.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; AGENT="${AGENT:-saba}"; API="$HERE/trinity_api.sh"
echo "agent: $("$API" GET "/api/agents/$AGENT" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("name"),d.get("status"))')"
resp="$("$API" POST "/api/agents/$AGENT/task" '{"message":"/academy-cycle","async_mode":true}')"
eid="$(printf '%s' "$resp" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("execution_id") or d.get("id") or "")')"
[ -n "$eid" ] || { echo "could not read execution id from response: $resp" >&2; exit 1; }
echo "execution: $eid (waiting, up to ${MAX_WAIT:-2400}s)"
end=$(( $(date +%s) + ${MAX_WAIT:-2400} ))
while :; do
  st="$("$API" GET "/api/agents/$AGENT/executions/$eid" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("status"))')"
  case "$st" in running|queued|pending|started) ;; *) break;; esac
  [ "$(date +%s)" -lt "$end" ] || { echo "still $st after the wait limit; follow it in the Trinity UI" >&2; exit 3; }
  sleep 10
done
echo "finished with status: $st"
"$API" GET "/api/agents/$AGENT/executions/$eid/log" | python3 "$HERE/show_execution.py"
