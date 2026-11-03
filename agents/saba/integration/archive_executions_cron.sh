#!/usr/bin/env bash
# Cron entry point for the daily archive of Saba's Trinity execution logs (host maintenance; unrelated to Saba's research cycles or schedules).
#   crontab:  17 4  * * *  /path/to/agents/saba/integration/archive_executions_cron.sh
#             17 16 * * *  /path/to/agents/saba/integration/archive_executions_cron.sh --if-stale 20      # catch-up if the 04:17 run was missed (host off) or failed
# - one run at a time (flock), umask 077, output appended to <archive parent>/archive.log (trimmed to the last 2000 lines)
# - a failure leaves <archive>/ALERT, a syslog line (tag saba-audit) and a non-zero exit; see ACADEMY_AUDIT.md
set -uo pipefail
umask 077
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${SABA_AUDIT_DIR:-$HOME/.local/share/saba-audit}"
OUT="$BASE/trinity-executions"
LOG="$BASE/archive.log"
mkdir -p "$BASE" && chmod 700 "$BASE"
exec 9>"$BASE/.cron.lock"
if ! flock -n 9; then echo "$(date -u +%FT%TZ) skipped: previous run still active" >> "$LOG"; exit 0; fi
export PATH="/usr/local/bin:/usr/bin:/bin"
{
  echo "=== $(date -u +%FT%TZ) archive $*"
  python3 "$HERE/archive_executions.py" --out "$OUT" "$@"
  rc=$?
  echo "exit $rc"
} >> "$LOG" 2>&1
rc=$(tail -n1 "$LOG" | awk '{print $2}')
[ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ] && tail -n 1500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
chmod 600 "$LOG" 2>/dev/null
exit "${rc:-1}"
