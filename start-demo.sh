#!/usr/bin/env bash
# Spin up clip-factory locally and expose it through a Cloudflare Quick Tunnel.
# Use when the hosted demo is down. Kill with Ctrl+C when done.
#
#   ./start-demo.sh                 # demo mode: no upload, no GPT calls
#   DEMO_MODE=0 ./start-demo.sh     # full app with your OpenAI key (anyone with the URL can spend it)
#
# What it does:
#   1. Starts uvicorn on http://127.0.0.1:8765 (DEMO_MODE=1 unless DEMO_MODE=0 is set)
#   2. Opens a one-shot cloudflared tunnel and prints the public https URL
#   3. Both processes terminate together on Ctrl+C
set -euo pipefail
export DEMO_MODE="${DEMO_MODE:-1}"

cd "$(dirname "$0")"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared not installed. Run: brew install cloudflared"
  exit 1
fi

if [ "$DEMO_MODE" = "0" ]; then
  if [ -z "${OPENAI_API_KEY:-}" ] && [ -f "$HOME/.config/clip-factory/key" ]; then
    export OPENAI_API_KEY="$(cat "$HOME/.config/clip-factory/key")"
  fi
fi

PORT="${PORT:-8765}"

echo "-> Starting uvicorn on http://127.0.0.1:$PORT (DEMO_MODE=$DEMO_MODE)"
python3 -m uvicorn app:app --host 127.0.0.1 --port "$PORT" > /tmp/factory-uvicorn.log 2>&1 &
UVICORN_PID=$!

cleanup() {
  echo
  echo "-> Stopping (uvicorn pid $UVICORN_PID, cloudflared pid ${TUNNEL_PID:-?})"
  kill "$UVICORN_PID" 2>/dev/null || true
  kill "${TUNNEL_PID:-0}" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for i in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:$PORT/api/sessions" >/dev/null 2>&1; then break; fi
  sleep 0.3
done

echo "-> Opening Cloudflare Quick Tunnel - the public URL prints below in ~3 seconds"
echo "  Share that URL with whoever needs it. Ctrl+C here when done."
echo "---------------------------------------------------------------------"

cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" 2>&1 | tee /tmp/factory-tunnel.log &
TUNNEL_PID=$!
wait "$TUNNEL_PID"
