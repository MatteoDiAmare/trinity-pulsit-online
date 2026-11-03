#!/usr/bin/env bash
# Waits until an absolute UTC time, then runs the given command. Usage: integration/run_at.sh 2026-11-01T18:50:00Z command args...
set -euo pipefail
T="$(date -u -d "${1:?ISO time}" +%s)"; shift
while [ "$(date -u +%s)" -lt "$T" ]; do sleep 30; done
exec "$@"
