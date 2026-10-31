#!/usr/bin/env bash
# Creates the Academy schedule on the existing Saba through Trinity - DISABLED unless you pass --enable.
# Needs a prior integration/trinity_login.sh (token file; nothing is read from containers or environments).
#   integration/register_with_trinity.sh            # creates the schedule disabled (max 4 cycles/day: 0 */6 * * * UTC)
#   integration/register_with_trinity.sh --enable   # only after the first autonomous cycle has been verified
# Env: AGENT (default saba-pulsit-online), CRON (default "0 */6 * * *")
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; API="$HERE/trinity_api.sh"; AGENT="${AGENT:-saba}"; CRON="${CRON:-0 */6 * * *}"
ENABLED=false; [ "${1:-}" = "--enable" ] && ENABLED=true
sched="$("$API" GET "/api/agents/$AGENT/schedules")"
id="$(printf '%s' "$sched" | python3 -c 'import json,sys;print(" ".join(s["id"] for s in json.load(sys.stdin) if s["name"]=="Academy research cycle"))')"
if [ -z "$id" ]; then
  body="$(ENABLED=$ENABLED CRON="$CRON" python3 -c 'import json,os;print(json.dumps({"name":"Academy research cycle","cron_expression":os.environ["CRON"],"message":"/academy-cycle","timezone":"UTC","enabled":os.environ["ENABLED"]=="true","description":"Autonomous Academy research cycle (max 4/day; budget enforced by the bridge)","max_retries":0}))')"
  "$API" POST "/api/agents/$AGENT/schedules" "$body" | python3 -c 'import json,sys;d=json.load(sys.stdin);print("created schedule",d.get("id"),"cron",d.get("cron_expression"),"enabled",d.get("enabled"))'
elif [ "$ENABLED" = true ]; then
  "$API" POST "/api/agents/$AGENT/schedules/$id/enable" >/dev/null && echo "schedule $id enabled"
else
  echo "schedule $id already exists; use --enable to switch it on"
fi
