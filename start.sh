#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT_DIR"

if [ ! -x "$ROOT_DIR/.venv/bin/python" ] || ! "$ROOT_DIR/.venv/bin/python" -c 'import streamlit, uvicorn' >/dev/null 2>&1; then
  if ! command -v uv >/dev/null 2>&1; then
    echo "Error: uv is required to create the project environment. Install it from https://docs.astral.sh/uv/" >&2
    exit 1
  fi
  echo "Project environment is missing or incomplete; syncing dependencies from uv.lock..."
  uv sync --frozen
fi

if ! "$ROOT_DIR/.venv/bin/python" -c 'import streamlit, uvicorn' >/dev/null 2>&1; then
  echo "Error: dependencies are incomplete. Run 'uv sync --frozen' successfully, then retry." >&2
  exit 1
fi

cleanup() {
  trap - EXIT INT TERM
  for child_pid in "${API_PID:-}" "${WEBUI_PID:-}"; do
    if [ -n "$child_pid" ]; then
      kill -TERM "$child_pid" 2>/dev/null || true
    fi
  done
  for child_pid in "${API_PID:-}" "${WEBUI_PID:-}"; do
    if [ -n "$child_pid" ]; then
      wait "$child_pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Starting API at http://127.0.0.1:8080"
"$ROOT_DIR/.venv/bin/python" main.py &
API_PID=$!

echo "Starting WebUI..."
sh "$ROOT_DIR/webui.sh" &
WEBUI_PID=$!

wait -n "$API_PID" "$WEBUI_PID"
exit $?
