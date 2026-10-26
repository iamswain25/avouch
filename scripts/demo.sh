#!/usr/bin/env bash
# One command to a working board: LocalNet -> contract -> D1 -> edge -> one seeded answer.
#
#   ./scripts/demo.sh          # start everything and leave the edge running
#   ./scripts/demo-stop.sh     # stop the edge and LocalNet
#
# Nothing here touches a network account or a secret: localnet keys come from KMD, the edge
# bindings are dev values, and the D1 database is the local sqlite file.
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-8789}"
BASE="http://127.0.0.1:${PORT}"
LOG="${LOG:-/tmp/avouch-demo.log}"
PIDFILE=".demo-edge.pid"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 1; }; }
need algokit
need uv
need npx

echo "== 1/5 chain"
if ! algokit localnet status 2>/dev/null | grep -q "Status: Running"; then
  if ! algokit localnet start >/tmp/avouch-localnet.log 2>&1; then
    echo "localnet failed to start. If this machine has run the sandbox before, run:" >&2
    echo "  algokit localnet reset && ./scripts/demo.sh" >&2
    tail -5 /tmp/avouch-localnet.log >&2 || true
    exit 1
  fi
fi
echo "   localnet running (algod http://localhost:4001)"

echo "== 2/5 contract"
eval "$(uv run --quiet --with py-algorand-sdk python scripts/deploy_slot_localnet.py --export)"
printf 'APP_ID=%s\nAPP_ADDRESS=%s\nTREASURY=%s\nPORT=%s\n' "$APP_ID" "$APP_ADDRESS" "$TREASURY" "$PORT" > .demo-env
echo "   app ${APP_ID}"

echo "== 3/5 storage"
# A fresh demo starts from an empty local database: slot ids are chain state, and a stale row from
# an earlier app id would report a body for a slot this chain knows nothing about.
rm -rf .wrangler/state/v3/d1
npx --yes wrangler@latest d1 execute avouch-board --local --file=schema.sql >/tmp/avouch-d1.log 2>&1
echo "   local D1 schema applied (state reset)"

echo "== 4/5 edge"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill "$(cat "$PIDFILE")" 2>/dev/null || true
  sleep 2
fi
npx --yes wrangler@latest pages dev site --port "$PORT" \
  --binding X402_SECRET=devsecret --binding PAY_TO="$TREASURY" \
  --binding ALGOD_URL=http://localhost:4001 \
  --binding ALGOD_TOKEN=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --binding APP_ID="$APP_ID" --binding APP_ADDRESS="$APP_ADDRESS" --binding TREASURY="$TREASURY" \
  --binding X402_NETWORK=algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDe \
  >"$LOG" 2>&1 &
echo $! >"$PIDFILE"

for _ in $(seq 1 90); do
  if curl -fsS -A 'avouch-demo/1.0' "$BASE/api/ping" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS -A 'avouch-demo/1.0' "$BASE/api/ping" >/dev/null 2>&1 || {
  echo "edge did not come up; last lines of $LOG:" >&2; tail -20 "$LOG" >&2; exit 1;
}
echo "   edge on $BASE (log: $LOG)"

echo "== 5/5 seed"
uv run --quiet --with py-algorand-sdk python scripts/seed_localnet.py --base "$BASE" --slot 1 --pay-read

cat <<EOF

Board is up.
  index   $BASE/api/posts
  answer  $BASE/api/posts/1      (402 until you pay; 1,000 uALGO)
  meta    $BASE/api/meta
  mcp     POST $BASE/mcp        (initialize, then tools/list)
Stop with: ./scripts/demo-stop.sh
EOF
