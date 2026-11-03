#!/usr/bin/env bash
# Cron entry point for the verified host-side backup of the Trinity database (NOT scheduled until the operator approves it).
# Proposed crontab (the host is often suspended at the nightly time, so the catch-up runs only act when the last backup is older than 20 h):
#   47 4 * * *       /path/to/backup_trinity_db_cron.sh
#   47 8-22/2 * * *  /path/to/backup_trinity_db_cron.sh --if-stale 20
# One run at a time (flock), umask 077, output to <audit dir>/db-backup.log; a failure leaves <backups>/ALERT, a syslog line and a non-zero exit.
set -uo pipefail
umask 077
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${SABA_AUDIT_DIR:-$HOME/.local/share/saba-audit}"
OUT="$BASE/trinity-db-backups"
LOG="$BASE/db-backup.log"
mkdir -p "$BASE" && chmod 700 "$BASE"
exec 9>"$BASE/.cron-dbbackup.lock"
if ! flock -n 9; then echo "$(date -u +%FT%TZ) skipped: previous run still active" >> "$LOG"; exit 0; fi
export PATH="/usr/local/bin:/usr/bin:/bin"
{
  echo "=== $(date -u +%FT%TZ) db backup $*"
  python3 "$HERE/backup_trinity_db.py" --out "$OUT" "$@"
  rc=$?
  echo "exit $rc"
} >> "$LOG" 2>&1
rc=$(tail -n1 "$LOG" | awk '{print $2}')
[ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ] && tail -n 1500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
chmod 600 "$LOG" 2>/dev/null
exit "${rc:-1}"
