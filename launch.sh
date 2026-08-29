#!/usr/bin/env bash
# One-action local launcher (Tsenta Remaining-Gaps Closure V2, section 2).
#
# Target experience: USER OPENS ONE LAUNCHER -> Sponsor Job Agent starts
# safely -> local server becomes ready -> browser opens the dashboard ->
# user clicks Start Agent. No `python`/`uvicorn`/`git`/DB command typed by
# hand, no raw developer stack trace shown on failure.
#
# Usage:
#   ./launch.sh            (same as "start")
#   ./launch.sh start
#   ./launch.sh stop
#   ./launch.sh restart
#   ./launch.sh status
#
# This script never embeds a credential and never hardcodes a path outside
# this repository -- everything is relative to its own location (works the
# same for any user, any checkout path).

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

HOST="${SPONSOR_AGENT_HOST:-127.0.0.1}"
PORT="${SPONSOR_AGENT_PORT:-8000}"
PIDFILE="data/launcher.pid"
LOGDIR="logs"
SERVER_LOG="$LOGDIR/server.log"
DASHBOARD_URL="http://${HOST}:${PORT}"

mkdir -p data output candidate_data "$LOGDIR"

_venv_python() {
  if [ -x ".venv/bin/python" ]; then
    echo ".venv/bin/python"
  else
    echo "python3"
  fi
}

_launcher_py() {
  # Runs app.launcher's CLI with the venv's own interpreter when it exists
  # (matching start.sh's own dependency set), falling back to system python3
  # so `status`/`stop` still work even before the venv has ever been
  # created.
  "$(_venv_python)" -m app.launcher "$@" --host "$HOST" --port "$PORT" 2>/dev/null
}

_open_browser() {
  # Best-effort only -- a failure here must never fail the whole launch;
  # the dashboard URL is always printed as a fallback.
  if grep -qi microsoft /proc/version 2>/dev/null; then
    ( explorer.exe "$DASHBOARD_URL" >/dev/null 2>&1 \
      || cmd.exe /c start "" "$DASHBOARD_URL" >/dev/null 2>&1 \
      || powershell.exe -NoProfile -Command "Start-Process '$DASHBOARD_URL'" >/dev/null 2>&1 ) &
  elif command -v wslview >/dev/null 2>&1; then
    ( wslview "$DASHBOARD_URL" >/dev/null 2>&1 ) &
  elif command -v xdg-open >/dev/null 2>&1; then
    ( xdg-open "$DASHBOARD_URL" >/dev/null 2>&1 ) &
  elif command -v open >/dev/null 2>&1; then
    ( open "$DASHBOARD_URL" >/dev/null 2>&1 ) &
  fi
}

_pid_running() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

cmd_status() {
  local detect_json
  detect_json="$(_launcher_py check)"
  if echo "$detect_json" | grep -q '"ready": *true'; then
    echo "Sponsor Job Agent is RUNNING and ready at $DASHBOARD_URL"
    return 0
  fi
  echo "Sponsor Job Agent is STOPPED (not answering at $DASHBOARD_URL)"
  return 1
}

cmd_start() {
  if [ ! -f "app/main.py" ]; then
    echo "Sponsor Job Agent could not start: this doesn't look like the project directory ($REPO_DIR)."
    return 1
  fi

  local detect_json outcome
  detect_json="$(_launcher_py detect)"
  outcome="$(echo "$detect_json" | grep -o '"outcome": *"[A-Z_]*"' | sed 's/.*"\([A-Z_]*\)"$/\1/')"

  if [ "$outcome" = "ALREADY_RUNNING" ]; then
    echo "Sponsor Job Agent is already running at $DASHBOARD_URL -- opening your browser."
    _open_browser
    return 0
  fi
  if [ "$outcome" = "PORT_CONFLICT" ]; then
    echo "Sponsor Job Agent could not start: something else is already using port $PORT."
    echo "Close whatever is using that port, or set SPONSOR_AGENT_PORT to a different one, and try again."
    return 1
  fi

  if [ -f "$PIDFILE" ]; then
    local old_pid
    old_pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if _pid_running "$old_pid"; then
      echo "Sponsor Job Agent already appears to be starting (pid $old_pid) -- waiting for it instead of starting a second copy."
    else
      rm -f "$PIDFILE"
    fi
  fi

  if [ ! -f "$PIDFILE" ]; then
    echo "Starting Sponsor Job Agent..."
    nohup ./start.sh >>"$SERVER_LOG" 2>&1 &
    echo "$!" > "$PIDFILE"
  fi

  local wait_json ready
  wait_json="$(_launcher_py wait --timeout 90)"
  ready="$(echo "$wait_json" | grep -o '"outcome": *"[A-Z_]*"' | sed 's/.*"\([A-Z_]*\)"$/\1/')"

  if [ "$ready" = "STARTED" ]; then
    echo "Sponsor Job Agent is ready. Opening $DASHBOARD_URL ..."
    _open_browser
    return 0
  fi

  echo "Sponsor Job Agent could not start in time."
  echo "Details: $(echo "$wait_json" | grep -o '"detail": *"[^"]*"' | sed 's/.*"detail": *"\(.*\)"$/\1/')"
  echo "Full log: $SERVER_LOG"
  return 1
}

cmd_stop() {
  if [ ! -f "$PIDFILE" ]; then
    echo "Sponsor Job Agent is not running (no launcher pid file)."
    return 0
  fi
  local pid
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if ! _pid_running "$pid"; then
    echo "Sponsor Job Agent is not running (stale pid file removed)."
    rm -f "$PIDFILE"
    return 0
  fi

  echo "Stopping Sponsor Job Agent (pid $pid)..."
  kill -TERM "$pid" 2>/dev/null
  local waited=0
  while _pid_running "$pid" && [ "$waited" -lt 15 ]; do
    sleep 1
    waited=$((waited + 1))
  done
  if _pid_running "$pid"; then
    echo "Sponsor Job Agent did not stop gracefully -- forcing shutdown."
    kill -KILL "$pid" 2>/dev/null
  fi
  rm -f "$PIDFILE"
  echo "Stopped."
}

cmd_restart() {
  cmd_stop
  cmd_start
}

main() {
  local action="${1:-start}"
  case "$action" in
    start) cmd_start ;;
    stop) cmd_stop ;;
    restart) cmd_restart ;;
    status) cmd_status ;;
    *)
      echo "Usage: $0 {start|stop|restart|status}"
      return 1
      ;;
  esac
}

main "$@"
