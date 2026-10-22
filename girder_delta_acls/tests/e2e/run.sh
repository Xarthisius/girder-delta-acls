#!/usr/bin/env bash
# Browser end-to-end checks for the access-dialog UI.
#
# Deliberately NOT part of `tox -e pytest`: this needs a built web client, a running Girder,
# a Mongo it is allowed to drop, and a Chrome. Run it by hand after touching web_client/.
#
# Usage:  ./run.sh [/path/to/venv]      (defaults to the repo's uv-managed .venv)
#
# Env overrides: GIRDER_URL, GIRDER_PORT, GIRDER_DB, CHROME, PLAYWRIGHT, SHOT_DIR
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"

VENV="${1:-$REPO/.venv}"
PORT="${GIRDER_PORT:-8751}"
DB="${GIRDER_DB:-girder_delta_acls_ui}"
export GIRDER_URL="${GIRDER_URL:-http://localhost:$PORT}"

[ -x "$VENV/bin/girder" ] || { echo "no girder in $VENV -- run 'uv sync' or pass a venv path as \$1"; exit 2; }
[ -f "$REPO/girder_delta_acls/web_client/dist/girder-plugin-delta-acls.umd.cjs" ] \
  || { echo "web client not built: cd girder_delta_acls/web_client && npm ci && npm run build"; exit 2; }

# A fresh database each run: only the first user created in a database becomes a site
# admin, and the fixture needs one.
echo "== dropping $DB and starting girder on :$PORT"
"$VENV/bin/python" - <<PY
from pymongo import MongoClient
MongoClient('mongodb://localhost:27017').drop_database('$DB')
PY

"$VENV/bin/girder" serve -p "$PORT" -d "mongodb://localhost:27017/$DB" \
    --with-temp-assetstore > "$HERE/girder.log" 2>&1 &
SERVER=$!
# Do not use `pkill -f "girder serve"`: the pattern matches the calling shell's own argv.
trap 'kill $SERVER 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  if curl -sf -o /dev/null "$GIRDER_URL/api/v1/system/version"; then break; fi
  sleep 1
done
curl -sf -o /dev/null "$GIRDER_URL/api/v1/system/version" \
  || { echo "girder did not come up; see $HERE/girder.log"; exit 1; }

mkdir -p "${SHOT_DIR:-$HERE/shots}"
rc=0
node "$HERE/01-access-dialog.mjs" || rc=1
echo
echo "screenshots in ${SHOT_DIR:-$HERE/shots}"
exit $rc
