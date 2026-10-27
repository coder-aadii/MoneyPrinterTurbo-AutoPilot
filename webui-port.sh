#!/usr/bin/env sh

port_available() {
  WEBUI_HOST="$MPT_WEBUI_HOST" WEBUI_PORT="$MPT_WEBUI_PORT" python3 - <<'PY' 2>/dev/null
import os
import socket
import sys

host = os.environ.get("WEBUI_HOST", "127.0.0.1")
port = int(os.environ.get("WEBUI_PORT", "8501"))
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    # A closed server can leave TCP connections in TIME_WAIT. Allow normal
    # address reuse so those stale connections do not look like a live listener.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sys.exit(1)
sys.exit(0)
PY
}

port_owner_pids() {
  owners=""
  if command -v lsof >/dev/null 2>&1; then
    owners=$(lsof -t -nP -iTCP:"$MPT_WEBUI_PORT" -sTCP:LISTEN 2>/dev/null || true)
  fi
  if [ -z "$owners" ] && command -v fuser >/dev/null 2>&1; then
    owners=$(fuser -n tcp "$MPT_WEBUI_PORT" 2>/dev/null || true)
  fi
  printf '%s\n' "$owners" | tr '[:space:]' '\n' | awk '/^[0-9]+$/ { print }' | sort -un
}

stop_port_owner_prompt() {
  if port_available; then
    return 0
  fi
  if [ ! -t 0 ]; then
    echo "***** WebUI port $MPT_WEBUI_PORT is already in use on $MPT_WEBUI_HOST. *****" >&2
    echo "***** Run ./start.sh in a terminal to choose whether to stop its owner. *****" >&2
    return 1
  fi

  while ! port_available; do
    owners=$(port_owner_pids)
    echo "***** WebUI port $MPT_WEBUI_PORT is already in use on $MPT_WEBUI_HOST. *****"

    if [ -z "$owners" ]; then
      echo "Could not identify the listening process (lsof/fuser may be unavailable or permission-restricted)."
      printf "Enter r to check again, or q to quit: "
      IFS= read -r choice || return 1
      case "$choice" in
        r|R) continue ;;
        *) return 1 ;;
      esac
    fi

    echo "Choose the process to stop (only the selected process will be signaled):"
    index=0
    for pid in $owners; do
      index=$((index + 1))
      details=$(ps -p "$pid" -o pid= -o comm= -o args= 2>/dev/null || true)
      [ -n "$details" ] || details="$pid (process details unavailable)"
      printf "  %s) %s\n" "$index" "$details"
    done
    printf "Enter a process number to stop it, r to rescan, or q to quit: "
    IFS= read -r choice || return 1
    case "$choice" in
      r|R) continue ;;
      q|Q) return 1 ;;
      *[!0-9]*|'') echo "Invalid selection."; continue ;;
    esac

    selected_pid=""
    index=0
    for pid in $owners; do
      index=$((index + 1))
      if [ "$index" = "$choice" ]; then
        selected_pid=$pid
        break
      fi
    done
    if [ -z "$selected_pid" ]; then
      echo "Invalid process number."
      continue
    fi

    if ! kill -TERM "$selected_pid" 2>/dev/null; then
      echo "Could not stop process $selected_pid. It may belong to another user; no elevated command was run."
      continue
    fi
    echo "Sent a normal stop request to process $selected_pid; waiting for the port to be released..."
    waited=0
    while [ "$waited" -lt 5 ] && ! port_available; do
      sleep 1
      waited=$((waited + 1))
    done
    if ! port_available; then
      printf "Process $selected_pid did not exit. Send force-kill (SIGKILL)? [y/N]: "
      IFS= read -r force_choice || return 1
      case "$force_choice" in
        y|Y|yes|YES)
          if ! kill -KILL "$selected_pid" 2>/dev/null; then
            echo "Could not force-stop process $selected_pid."
            continue
          fi
          waited=0
          while [ "$waited" -lt 5 ] && ! port_available; do
            sleep 1
            waited=$((waited + 1))
          done
          ;;
        *) echo "Leaving process $selected_pid running."; continue ;;
      esac
    fi
  done
}
