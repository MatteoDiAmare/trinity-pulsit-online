#!/usr/bin/env bash
# Host-side deploy of the Academy runtime into Saba's Trinity container (persistent volume only).
# Usage: integration/deploy_academy_to_container.sh [container=agent-saba] [git-ref=HEAD]
# - Copies a clean `git archive` of agents/saba at <ref> (no history, no secrets) into the container,
#   stamps ACADEMY_CODE_VERSION with the commit, creates a venv on the volume and installs pyarrow/yfinance.
# - Touches only /home/developer/.local/share/saba (volume-backed, git-ignored there). Idempotent.
# - Uses `docker cp`/`docker exec` only; no credentials are read or passed.
set -euo pipefail
CONTAINER="${1:-agent-saba}"
REF="${2:-HEAD}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(git -C "$HERE" rev-parse --show-toplevel)"
SHA="$(git -C "$REPO_ROOT" rev-parse "$REF")"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
git -C "$REPO_ROOT" archive "$SHA" agents/saba | tar -x -C "$STAGE"
echo "$SHA" > "$STAGE/agents/saba/ACADEMY_CODE_VERSION"
tar -C "$STAGE/agents" -cf "$STAGE/academy.tar" saba
CHOME="$(docker exec "$CONTAINER" bash -c 'echo $HOME')"      # /tmp in agent containers is a tmpfs that `docker cp` cannot write to
docker exec "$CONTAINER" mkdir -p "$CHOME/.tmp"
docker cp "$STAGE/academy.tar" "$CONTAINER:$CHOME/.tmp/academy.tar"
docker exec -i "$CONTAINER" bash -s <<'INNER'
set -euo pipefail
BASE="$HOME/.local/share/saba"
mkdir -p "$BASE" "$BASE/market_data"
rm -rf "$BASE/academy.new" && mkdir "$BASE/academy.new"
tar -xf "$HOME/.tmp/academy.tar" -C "$BASE/academy.new" --strip-components=1
rm -f "$HOME/.tmp/academy.tar"
# fully self-contained venv (the sandbox worker runs `python -I`, which ignores the user site); rebuild an older one that used system packages
if [ -f "$BASE/venv/pyvenv.cfg" ] && grep -q "include-system-site-packages = true" "$BASE/venv/pyvenv.cfg"; then rm -rf "$BASE/venv"; fi
[ -d "$BASE/venv" ] || python3 -m venv "$BASE/venv"
"$BASE/venv/bin/python" -m pip install --quiet --disable-pip-version-check "numpy>=2" "pandas>=2.2,<4" "pyarrow>=17" "yfinance>=0.2.65,<2"
rm -rf "$BASE/academy.old"
[ -d "$BASE/academy" ] && mv "$BASE/academy" "$BASE/academy.old"
mv "$BASE/academy.new" "$BASE/academy"
rm -rf "$BASE/academy.old"
cat > "$BASE/academy-bridge" <<'EOS'
#!/usr/bin/env bash
# Fixed entry point: one JSON object on stdin, one on stdout. Research only, no orders.
BASE="$HOME/.local/share/saba"
cd "$BASE/academy"
exec "$BASE/venv/bin/python" -m market_data.academy_bridge --root "$BASE/market_data" --strategies-dir "$HOME/strategies" "$@"
EOS
chmod 755 "$BASE/academy-bridge"
mkdir -p "$HOME/strategies/code"
cp -n "$BASE"/academy/strategies/*.json "$HOME/strategies/" 2>/dev/null || true   # seed examples once; Saba's own specs live in her repo
echo "installed academy $(cat "$BASE/academy/ACADEMY_CODE_VERSION") into $BASE"
INNER
