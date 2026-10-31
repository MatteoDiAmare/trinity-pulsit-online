#!/usr/bin/env bash
# Local login to Trinity. The password is read WITHOUT echo (or from a file you control); the token is written to a mode-600
# header file and is never printed, never put on a command line and never sent anywhere except to TRINITY_URL.
#   integration/trinity_login.sh                       # prompts in your terminal
#   integration/trinity_login.sh --password-file F     # F: mode 600 file holding only the password
# Env: TRINITY_URL (default http://localhost:8000), TRINITY_USER (default admin), SABA_AUTH_FILE (default ~/.config/saba-trinity/auth.hdr)
# The Trinity JWT lasts 7 days and is invalidated by a backend restart; run this again then.
set -euo pipefail
URL="${TRINITY_URL:-http://localhost:8000}"; USER_NAME="${TRINITY_USER:-admin}"
AUTH="${SABA_AUTH_FILE:-$HOME/.config/saba-trinity/auth.hdr}"
case "$URL" in http://localhost*|http://127.0.0.1*|https://*) ;; *) echo "refusing to send credentials over plain http to $URL" >&2; exit 2;; esac
umask 077; mkdir -p "$(dirname "$AUTH")"
if [ "${1:-}" = "--password-file" ]; then
  f="${2:?path}"; [ "$(stat -c %a "$f")" = "600" ] || { echo "password file must have mode 600" >&2; exit 2; }
  PW="$(cat "$f")"
else
  [ -r /dev/tty ] || { echo "no terminal available: run this in your own terminal, or use --password-file" >&2; exit 2; }
  read -rsp "Trinity password for $USER_NAME (input hidden): " PW </dev/tty; echo >&2
fi
tmp="$(mktemp)"; trap 'rm -f "$tmp"; unset PW' EXIT
# username/password go through a curl config on stdin, so they never appear in the process list
code=$(printf 'data-urlencode = "username=%s"\ndata-urlencode = "password=%s"\n' "$USER_NAME" "$PW" \
  | curl -sS -m 20 -o "$tmp" -w '%{http_code}' -K - -X POST "$URL/api/token")
if [ "$code" != "200" ]; then echo "login failed (HTTP $code)" >&2; exit 1; fi
python3 - "$tmp" "$AUTH" <<'PY'
import json, os, sys
tok = json.load(open(sys.argv[1])).get("access_token", "")
if not tok:
    sys.exit("login response had no token")
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(f"Authorization: Bearer {tok}\n")
PY
echo "logged in; token stored in $AUTH (mode 600, not displayed). Valid for about 7 days."
