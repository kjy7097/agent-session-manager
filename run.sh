#!/usr/bin/env bash
# Phase 1 MVP launcher: starts the local agent + control plane and prints the URL.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

cleanup() { kill "${AGENT_PID:-}" "${CTRL_PID:-}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# Generate config (random secret) on first run, and read the control URL.
"$PY" -c "from csm.config import load_config; load_config()"
read -r CTRL_URL < <("$PY" -c "import json;c=json.load(open('csm.config.json'));print(f\"http://{c['control']['host']}:{c['control']['port']}\")")

echo "starting csm-agent…"
"$PY" -m csm.agent &
AGENT_PID=$!
sleep 1

echo "starting csm-control…"
"$PY" -m csm.control &
CTRL_PID=$!
sleep 1

echo
echo "  ▶ Claude Session Manager:  $CTRL_URL"
echo "  (Ctrl-C to stop)"
echo
wait
