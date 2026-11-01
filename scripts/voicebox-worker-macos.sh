#!/bin/bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
BASE="$HOME/Library/Application Support/Voicebox Remote Worker"
CONFIG="$HOME/.config/voicebox/remote-worker.json"
AGENT="$BASE/voicebox_remote_worker.py"
VENV="$BASE/venv"
SERVER="/Applications/Voicebox.app/Contents/MacOS/voicebox-server"
PORT="17494"
DATA="$BASE/data"
LOG="$BASE/worker.log"

PYTHON_BIN="${VOICEBOX_WORKER_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /usr/local/bin/python3.14 /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/bin/python3; do
    if [[ -x "$candidate" ]] && "$candidate" -c 'import plistlib, venv' >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
if [[ -z "$PYTHON_BIN" ]] || ! "$PYTHON_BIN" -c 'import plistlib, venv' >/dev/null 2>&1; then
  echo "A working Python with venv and httpx is required. Install Python 3.12 or newer, then restart this worker." >&2
  exit 1
fi

mkdir -p "$BASE" "$DATA" "$HOME/.config/voicebox"
chmod 700 "$BASE" "$HOME/.config/voicebox"
if [[ ! -x "$SERVER" ]]; then
  echo "Voicebox.app server not found at $SERVER" >&2
  exit 1
fi
if [[ ! -f "$CONFIG" ]]; then
  echo "Enroll this device first; credentials are missing at $CONFIG" >&2
  exit 1
fi
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade httpx
fi
chmod 600 "$CONFIG"
SERVER_PID=""
cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if ! curl --silent --fail --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null; then
  VOICEBOX_MODELS_DIR="$HOME/.cache/huggingface/hub" \
    "$SERVER" --host 127.0.0.1 --port "$PORT" --data-dir "$DATA" >>"$LOG" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 180); do
    if curl --silent --fail --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null; then break; fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "The local Voicebox inference server stopped during startup. See $LOG" >&2
      exit 1
    fi
    sleep 2
  done
fi

if ! curl --silent --fail --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null; then
  echo "Local Voicebox did not become healthy on port $PORT. See $LOG" >&2
  exit 1
fi

"$VENV/bin/python" "$AGENT" \
  --credential-file "$CONFIG" --local-api "http://127.0.0.1:${PORT}" \
  --state-dir "$BASE/state" >>"$LOG" 2>&1
