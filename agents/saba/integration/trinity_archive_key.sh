#!/usr/bin/env bash
# Create, inspect or revoke the READ-ONLY Trinity key that the execution-log archiver uses instead of your admin login.
#   integration/trinity_archive_key.sh create  [--password-file F] [--force]   # run by YOU, in your own terminal (interactive login)
#   integration/trinity_archive_key.sh revoke  [--password-file F]             # re-login, revoke the key recorded in archive.meta
#   integration/trinity_archive_key.sh info                                    # key id / age, no secret
# Prerequisite (separate, explicitly approved deployment): the backend runs with OPS_LOG_READ_AGENTS=<agent> (Trinity PR "ops keys:
# opt-in per-agent read of finished execution transcripts"), otherwise the key can list runs but not read their logs.
# The key (scope `ops`: read-only, fenced routes, admin-minted, revocable, no expiry) is written straight to a mode-600 header file and
# is never printed, put on a command line or logged. Only the key id and creation time go into archive.meta (no secret).
# Env: TRINITY_URL (default http://localhost:8000), TRINITY_USER (default admin), SABA_ARCHIVE_KEY_FILE (default ~/.config/saba-trinity/archive.hdr)
set -euo pipefail
URL="${TRINITY_URL:-http://localhost:8000}"; USER_NAME="${TRINITY_USER:-admin}"
KEY="${SABA_ARCHIVE_KEY_FILE:-$HOME/.config/saba-trinity/archive.hdr}"; META="${KEY%.hdr}.meta"
MODE="${1:-info}"; shift || true
case "$URL" in http://localhost*|http://127.0.0.1*|https://*) ;; *) echo "refusing to send credentials over plain http to $URL" >&2; exit 2;; esac
PWFILE=""; FORCE=0
while [ $# -gt 0 ]; do case "$1" in --password-file) PWFILE="${2:?path}"; shift 2;; --force) FORCE=1; shift;; *) echo "unknown option $1" >&2; exit 2;; esac; done
umask 077; mkdir -p "$(dirname "$KEY")"

if [ "$MODE" = "info" ]; then
  [ -f "$META" ] || { echo "no archive key recorded ($META missing)"; exit 1; }
  python3 - "$META" "$KEY" <<'PY'
import json, os, sys, datetime as dt
m = json.load(open(sys.argv[1])); made = dt.datetime.fromisoformat(m["created_at"])
age = (dt.datetime.now(dt.timezone.utc) - made).days
print(json.dumps({"key_id": m["key_id"], "name": m["name"], "scope": m["scope"], "created_at": m["created_at"], "age_days": age,
                  "rotate_after_days": 90, "key_file_present": os.path.exists(sys.argv[2]),
                  "key_file_mode": oct(os.stat(sys.argv[2]).st_mode & 0o777) if os.path.exists(sys.argv[2]) else None}, indent=1))
PY
  exit 0
fi
[ "$MODE" = "create" ] || [ "$MODE" = "revoke" ] || { echo "usage: $0 create|revoke|info" >&2; exit 2; }
if [ "$MODE" = "create" ] && [ -e "$KEY" ] && [ "$FORCE" != 1 ]; then echo "$KEY already exists; use --force to replace it (revoke the old key first)" >&2; exit 2; fi
if [ -n "$PWFILE" ]; then
  [ "$(stat -c %a "$PWFILE")" = "600" ] || { echo "password file must have mode 600" >&2; exit 2; }
  PW="$(cat "$PWFILE")"
else
  [ -r /dev/tty ] || { echo "no terminal available: run this in your own terminal, or use --password-file" >&2; exit 2; }
  read -rsp "Trinity password for $USER_NAME (input hidden): " PW </dev/tty; echo >&2
fi
tmp="$(mktemp)"; tok="$(mktemp)"; trap 'rm -f "$tmp" "$tok"; unset PW' EXIT
code=$(printf 'data-urlencode = "username=%s"\ndata-urlencode = "password=%s"\n' "$USER_NAME" "$PW" \
  | curl -sS -m 20 -o "$tmp" -w '%{http_code}' -K - -X POST "$URL/api/token")
[ "$code" = "200" ] || { echo "login failed (HTTP $code)" >&2; exit 1; }
python3 - "$tmp" "$tok" <<'PY'
import json, os, sys
t = json.load(open(sys.argv[1])).get("access_token", "")
if not t: sys.exit("login response had no token")
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f: f.write(f"Authorization: Bearer {t}\n")
PY
if [ "$MODE" = "create" ]; then
  body='{"name":"saba-audit-archiver","description":"Read-only archiver of Saba execution logs (host cron). Revoke on suspicion; rotate every 90 days.","scope":"ops"}'
  code=$(curl -sS -m 20 -o "$tmp" -w '%{http_code}' -H "@$tok" -H "Content-Type: application/json" -X POST -d "$body" "$URL/api/mcp/keys")
  [ "$code" = "200" ] || { echo "key creation failed (HTTP $code): the response is not shown because it may contain secrets" >&2; exit 1; }
  python3 - "$tmp" "$KEY" "$META" <<'PY'
import datetime as dt, json, os, sys
r = json.load(open(sys.argv[1]))
key = r.get("api_key", "")
if not key.startswith("trinity_mcp_") or r.get("scope") != "ops" or not r.get("id"):
    sys.exit("unexpected response (no ops key returned); nothing written")
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f: f.write(f"Authorization: Bearer {key}\n")
fd = os.open(sys.argv[3], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"key_id": r["id"], "name": r.get("name"), "scope": r["scope"], "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}, f)
PY
  echo "ops key created for the archiver; stored in $KEY (mode 600, not displayed). Rotate after 90 days; revoke with: $0 revoke"
else
  [ -f "$META" ] || { echo "no key recorded in $META" >&2; exit 1; }
  kid=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['key_id'])" "$META")
  code=$(curl -sS -m 20 -o /dev/null -w '%{http_code}' -H "@$tok" -X POST "$URL/api/mcp/keys/$kid/revoke")
  [ "$code" = "200" ] || { echo "revoke failed (HTTP $code)" >&2; exit 1; }
  mv "$KEY" "$KEY.revoked" 2>/dev/null || true
  echo "key $kid revoked; the key file was renamed to $KEY.revoked (delete it when you no longer need it)"
fi
