#!/usr/bin/env bash
# 启动 Vue3 管理后台（Vite 开发服务器，默认 5173）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/web"
mkdir -p "$ROOT/logs" "$ROOT/.run"

if [ ! -d node_modules ]; then
  echo "首次启动，安装前端依赖..."
  npm install
fi

if [ -f "$ROOT/.run/web.pid" ] && kill -0 "$(cat "$ROOT/.run/web.pid")" 2>/dev/null; then
  echo "前端已在运行：http://127.0.0.1:5173"
  exit 0
fi

nohup npm run dev -- --host 127.0.0.1 --port 5173 >"$ROOT/logs/web.log" 2>&1 &
echo $! > "$ROOT/.run/web.pid"
sleep 3
echo "前端已启动：http://127.0.0.1:5173（日志 logs/web.log）"
