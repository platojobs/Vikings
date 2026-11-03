#!/usr/bin/env bash
# 一键停止：网关 + Mosquitto（PostgreSQL 默认保留，可用 --with-postgres 一并停止）
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT/.run"
PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@16/bin}"
PGDATA="${PGDATA:-/opt/homebrew/var/postgresql@16}"

stop_pid() {
  local name="$1" file="$2"
  if [ -f "$file" ]; then
    local pid
    pid="$(cat "$file")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null && echo "已停止 $name (pid=$pid)"
    fi
    rm -f "$file"
  else
    echo "$name 未记录 pid，可能未通过脚本启动"
  fi
}

stop_pid "网关" "$RUN_DIR/gateway.pid"
stop_pid "Mosquitto" "$RUN_DIR/mosquitto.pid"

if [ "${1:-}" = "--with-postgres" ]; then
  "$PG_BIN/pg_ctl" -D "$PGDATA" stop -m fast || true
fi
