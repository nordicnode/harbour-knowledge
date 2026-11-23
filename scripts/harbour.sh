#!/usr/bin/env bash
#
# Start and stop the local Harbour stack: Postgres, FastAPI, and Next.js.
#
#   ./scripts/harbour.sh start
#   ./scripts/harbour.sh status
#   ./scripts/harbour.sh logs api
#   ./scripts/harbour.sh stop
#
# The provider overlay defaults to .env.nim when it exists (the local
# NVIDIA NIM setup) and otherwise to .env. The API still reads .env for
# database settings through pydantic; the overlay only adds environment
# variables on top.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT/storage/run"
LOG_DIR="$RUN_DIR/logs"
API_DIR="$ROOT/services/api"
WEB_DIR="$ROOT/apps/web"
API_PID_FILE="$RUN_DIR/api.pid"
WEB_PID_FILE="$RUN_DIR/web.pid"
DB_MARKER="$RUN_DIR/postgres.started"
API_LOG="$LOG_DIR/api.log"
WEB_LOG="$LOG_DIR/web.log"

API_PORT="${HARBOUR_API_PORT:-8011}"
WEB_PORT=3000
API_URL="http://127.0.0.1:${API_PORT}"
WEB_URL="http://127.0.0.1:${WEB_PORT}"

if [[ -n "${HARBOUR_ENV_FILE:-}" ]]; then
  ENV_FILE="$HARBOUR_ENV_FILE"
elif [[ -f "$ROOT/.env.nim" ]]; then
  ENV_FILE="$ROOT/.env.nim"
else
  ENV_FILE="$ROOT/.env"
fi
[[ "$ENV_FILE" = /* ]] || ENV_FILE="$ROOT/$ENV_FILE"

BUN="$(command -v bun || true)"
PYTHON="$API_DIR/.venv/bin/python"
UV="$(command -v uv || true)"
SETSID="$(command -v setsid || true)"
CURL="$(command -v curl || true)"

mkdir -p "$RUN_DIR" "$LOG_DIR"

say() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: ./scripts/harbour.sh <command> [options]

Commands:
  start                  Start Postgres, the API, and the web app
  stop [--keep-db]       Stop the web app and API; stop Postgres only if
                         this script started it. --keep-db leaves Postgres up.
  stop --with-db         Also stop Postgres even if it was already running
  restart                Restart the web app and API; leave Postgres up
  status                 Show process and health status
  logs [api|web]         Follow the API or web log

Environment:
  HARBOUR_ENV_FILE       Provider overlay (default: .env.nim if present,
                         otherwise .env)
  HARBOUR_API_PORT       API port (default: 8011)
EOF
}

require_tools() {
  [[ -n "$CURL" ]] || die "curl is required."
  [[ -n "$SETSID" ]] || die "setsid is required to manage process groups."
}

pid_from_file() {
  [[ -f "$1" ]] || return 1
  local pid
  pid="$(tr -dc '0-9' < "$1")"
  [[ -n "$pid" ]] || return 1
  printf '%s\n' "$pid"
}

pid_alive() {
  local pid_file="$1" pid
  pid="$(pid_from_file "$pid_file" || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

port_open() {
  local port="$1"
  (exec 3<>"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1
}

http_ok() {
  "$CURL" -fsS --max-time 2 "$1" >/dev/null 2>&1
}

db_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' kb-postgres 2>/dev/null || true)" == "true" ]]
}

db_exists() {
  docker inspect kb-postgres >/dev/null 2>&1
}

stop_pidfile() {
  local name="$1" pid_file="$2" pattern="$3" pid cmd
  if [[ ! -f "$pid_file" ]]; then
    say "$name: not running"
    return 0
  fi

  pid="$(pid_from_file "$pid_file" || true)"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    say "$name: not running"
    return 0
  fi

  cmd="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  if [[ -n "$pattern" && ! "$cmd" =~ $pattern ]]; then
    warn "$name: pid $pid does not look like $pattern; leaving it alone."
    rm -f "$pid_file"
    return 0
  fi

  say "$name: stopping pid $pid"
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 50); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.2
  done
  if kill -0 "$pid" 2>/dev/null; then
    warn "$name: did not exit after 10 seconds; forcing it."
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    for _ in $(seq 1 25); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
  fi
  rm -f "$pid_file"
}

wait_http() {
  local name="$1" url="$2" pid="$3" log="$4"
  for _ in $(seq 1 240); do
    if http_ok "$url"; then
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      printf '\n%s exited during startup. Last log lines:\n' "$name" >&2
      tail -n 40 "$log" >&2 || true
      return 1
    fi
    sleep 0.5
  done
  printf '\n%s did not become ready at %s within 120 seconds. Last log lines:\n' "$name" "$url" >&2
  tail -n 40 "$log" >&2 || true
  return 1
}

start_db() {
  if db_running; then
    say "Postgres: already running"
    return 0
  fi
  [[ -f "$ROOT/.env" ]] || die ".env is missing. Copy .env.example to .env first."
  command -v docker >/dev/null 2>&1 || die "docker is required to start Postgres."
  say "Postgres: starting"
  make -C "$ROOT" db
  printf '1\n' > "$DB_MARKER"
}

start_api() {
  if pid_alive "$API_PID_FILE"; then
    if http_ok "$API_URL/healthz"; then
      say "API: already ready at $API_URL"
      return 0
    fi
    stop_pidfile "API" "$API_PID_FILE" 'uvicorn|app\.main'
  fi
  if port_open "$API_PORT"; then
    die "port $API_PORT is already in use by another process. Stop it or set HARBOUR_API_PORT."
  fi

  if [[ -x "$PYTHON" ]]; then
    local launcher=("$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT")
  else
    [[ -n "$UV" ]] || die "services/api/.venv is missing and uv is not installed."
    warn "services/api/.venv is missing; falling back to uv run."
    local launcher=("$UV" run uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT")
  fi

  say "API: starting on $API_URL with $(basename "$ENV_FILE")"
  {
    printf '\n=== %s starting API (port %s, env %s) ===\n' \
      "$(date -Is)" "$API_PORT" "$ENV_FILE"
  } >> "$API_LOG"
  setsid bash -c '
    set -euo pipefail
    cd "$1"
    set -a
    . "$2"
    set +a
    export API_PORT="$3"
    shift 3
    exec "$@"
  ' harbour-api "$API_DIR" "$ENV_FILE" "$API_PORT" "${launcher[@]}" >> "$API_LOG" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" > "$API_PID_FILE"

  if ! wait_http "API" "$API_URL/healthz" "$pid" "$API_LOG"; then
    stop_pidfile "API" "$API_PID_FILE" 'uvicorn|app\.main'
    return 1
  fi
  say "API: ready (pid $pid)"
}

start_web() {
  if pid_alive "$WEB_PID_FILE"; then
    if http_ok "$WEB_URL"; then
      say "Web: already ready at $WEB_URL"
      return 0
    fi
    stop_pidfile "Web" "$WEB_PID_FILE" 'next|bun'
  fi
  if port_open "$WEB_PORT"; then
    die "port $WEB_PORT is already in use by another process."
  fi
  [[ -n "$BUN" ]] || die "bun is required to start the web app."
  [[ -d "$WEB_DIR/node_modules" ]] || die "apps/web/node_modules is missing. Run: cd apps/web && bun install"

  say "Web: starting on $WEB_URL"
  {
    printf '\n=== %s starting web (port %s, API %s) ===\n' \
      "$(date -Is)" "$WEB_PORT" "$API_URL"
  } >> "$WEB_LOG"
  setsid bash -c '
    set -euo pipefail
    cd "$1"
    export API_URL="$2"
    shift 2
    exec "$@"
  ' harbour-web "$WEB_DIR" "$API_URL" "$BUN" run dev >> "$WEB_LOG" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" > "$WEB_PID_FILE"

  if ! wait_http "Web" "$WEB_URL" "$pid" "$WEB_LOG"; then
    stop_pidfile "Web" "$WEB_PID_FILE" 'next|bun'
    return 1
  fi
  say "Web: ready (pid $pid)"
}

check_nim_gateway() {
  [[ "$(basename "$ENV_FILE")" == ".env.nim" ]] || return 0
  if port_open 20128; then
    say "9router: listening on 127.0.0.1:20128"
  else
    warn "9router is not listening on 127.0.0.1:20128; NIM embeddings will fail."
  fi
}

start_stack() {
  require_tools
  [[ -f "$ENV_FILE" ]] || die "environment file not found: $ENV_FILE"
  start_db
  check_nim_gateway
  start_api
  if ! start_web; then
    stop_pidfile "API" "$API_PID_FILE" 'uvicorn|app\.main'
    exit 1
  fi
  printf '\nHarbour is ready:\n  Web  %s\n  API  %s\n' "$WEB_URL" "$API_URL"
  printf 'Logs: %s\n' "$LOG_DIR"
}

stop_stack() {
  local keep_db="${1:-0}" with_db="${2:-0}"
  stop_pidfile "Web" "$WEB_PID_FILE" 'next|bun'
  stop_pidfile "API" "$API_PID_FILE" 'uvicorn|app\.main'

  if [[ "$with_db" == "1" ]]; then
    if db_running; then
      say "Postgres: stopping"
      docker stop kb-postgres >/dev/null
    else
      say "Postgres: not running"
    fi
    rm -f "$DB_MARKER"
  elif [[ -f "$DB_MARKER" ]]; then
    if [[ "$keep_db" == "1" ]]; then
      say "Postgres: left running"
    elif db_running; then
      say "Postgres: stopping (started by this script)"
      docker stop kb-postgres >/dev/null
      rm -f "$DB_MARKER"
    else
      rm -f "$DB_MARKER"
      say "Postgres: not running"
    fi
  elif db_running; then
    say "Postgres: left running (it was already running before this script)"
  else
    say "Postgres: not running"
  fi
}

show_status() {
  printf 'Postgres: '
  if ! db_exists; then
    echo "not created"
  elif db_running; then
    echo "running"
  else
    echo "stopped"
  fi

  printf 'API:      '
  if http_ok "$API_URL/healthz"; then
    echo "ready at $API_URL"
  elif pid_alive "$API_PID_FILE"; then
    echo "process running, but not ready at $API_URL"
  else
    echo "stopped"
  fi

  printf 'Web:      '
  if http_ok "$WEB_URL"; then
    echo "ready at $WEB_URL"
  elif pid_alive "$WEB_PID_FILE"; then
    echo "process running, but not ready at $WEB_URL"
  else
    echo "stopped"
  fi

  if [[ "$(basename "$ENV_FILE")" == ".env.nim" ]]; then
    printf '9router:  '
    if port_open 20128; then echo "listening on 127.0.0.1:20128"; else echo "not listening"; fi
  fi
  printf 'Env:      %s\n' "$ENV_FILE"
  printf 'Logs:     %s\n' "$LOG_DIR"
}

follow_logs() {
  local target="${1:-api}"
  case "$target" in
    api) exec tail -n 80 -f "$API_LOG" ;;
    web) exec tail -n 80 -f "$WEB_LOG" ;;
    *) die "unknown log target '$target'; use api or web." ;;
  esac
}

command="${1:-}"
[[ -n "$command" ]] || { usage; exit 1; }
shift || true

case "$command" in
  start)
    start_stack
    ;;
  stop)
    keep_db=0
    with_db=0
    for arg in "$@"; do
      case "$arg" in
        --keep-db) keep_db=1 ;;
        --with-db) with_db=1 ;;
        *) die "unknown stop option '$arg'." ;;
      esac
    done
    stop_stack "$keep_db" "$with_db"
    ;;
  restart)
    stop_stack 1 0
    start_stack
    ;;
  status)
    show_status
    ;;
  logs)
    follow_logs "${1:-api}"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 1
    ;;
esac
