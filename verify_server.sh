#!/usr/bin/env bash
# Public boundary check. Does not authenticate to Monarch or print finance data.
set -euo pipefail

BASE="${1:?usage: verify_server.sh https://monarch-mcp.example.com}"
BASE="${BASE%/}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

request() {
  local label="$1" method="$2" path="$3"
  shift 3
  curl --silent --show-error --connect-timeout 5 --max-time 15 \
    -X "$method" -D "$TMP/$label.headers" -o "$TMP/$label.body" \
    -w '%{http_code}' "$@" "$BASE$path"
}
check_code() {
  local label="$1" got="$2" want="$3"
  if [ "$got" != "$want" ]; then
    echo "FAIL $label: HTTP $got, expected $want" >&2
    exit 1
  fi
  echo "PASS $label: HTTP $want"
}

status="$(request health GET /health)"
check_code /health "$status" 200
python3 - "$TMP/health.body" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    assert json.load(f) == {"status": "ok"}
PY

for path in /api/token /api/accounts /api/transaction/test; do
  status="$(request "removed-${path//\//-}" GET "$path")"
  check_code "$path" "$status" 404
done

status="$(request mcp POST /mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"ping"}')"
check_code '/mcp without auth' "$status" 401
if ! grep -qi '^www-authenticate:.*resource_metadata=' "$TMP/mcp.headers"; then
  echo 'FAIL /mcp: missing OAuth challenge' >&2
  exit 1
fi
echo 'PASS /mcp: OAuth challenge present'

status="$(request prm GET /.well-known/oauth-protected-resource/mcp)"
check_code 'OAuth protected-resource metadata' "$status" 200
python3 - "$TMP/prm.body" "$BASE/mcp" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    metadata = json.load(f)
assert metadata["resource"] == sys.argv[2], "OAuth resource URL mismatch"
assert metadata.get("authorization_servers"), "OAuth authorization server missing"
PY
echo 'PASS OAuth resource matches /mcp'
