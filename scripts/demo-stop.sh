#!/usr/bin/env bash
# Stop what scripts/demo.sh started. LocalNet is stopped only when --chain is passed, because a
# second board may be using the same sandbox.
set -euo pipefail

cd "$(dirname "$0")/.."
PIDFILE=".demo-edge.pid"

if [ -f "$PIDFILE" ]; then
  pid="$(cat "$PIDFILE")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    echo "edge stopped (pid $pid)"
  else
    echo "edge was not running"
  fi
  rm -f "$PIDFILE"
else
  echo "no pidfile; nothing started by scripts/demo.sh"
fi

if [ "${1:-}" = "--chain" ]; then
  algokit localnet stop >/dev/null 2>&1 || true
  echo "localnet stopped"
fi
