#!/usr/bin/env bash
# Thin authenticated curl for Trinity. Usage: integration/trinity_api.sh METHOD PATH [JSON_BODY]
# The token comes from the header file written by trinity_login.sh (curl -H @file: never on the command line).
set -euo pipefail
URL="${TRINITY_URL:-http://localhost:8000}"; AUTH="${SABA_AUTH_FILE:-$HOME/.config/saba-trinity/auth.hdr}"
[ -f "$AUTH" ] || { echo "not logged in: run integration/trinity_login.sh" >&2; exit 2; }
M="${1:?method}"; P="${2:?path}"; B="${3:-}"
args=(-sS -m "${CURL_MAX_TIME:-60}" -H "@$AUTH" -H "Content-Type: application/json" -X "$M" -w '\n%{http_code}')
[ -n "$B" ] && args+=(-d "$B")
out="$(curl "${args[@]}" "$URL$P")"; code="${out##*$'\n'}"; body="${out%$'\n'*}"
if [ "${code:0:1}" != "2" ]; then echo "HTTP $code: $body" >&2; exit 1; fi
printf '%s\n' "$body"
