#!/usr/bin/env bash
# ============================================================
# 一键启动：Mosquitto + PostgreSQL + Python 网关
# 用法：./scripts/start_all.sh
# 说明：前端管理后台另有 scripts/start_web.sh（Node 启动较慢，单独管理）
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MQ_BIN="${MQ_BIN:-/opt/homebrew/opt/mosquitto/sbin/mosquitto}"
PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@16/bin}"
PGDATA="${PGDATA:-/opt/homebrew/var/postgresql@16}"
PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3.14}"
RUN_DIR="$ROOT/.run"
LOG_DIR="$ROOT/logs"

mkdir -p "$RUN_DIR" "$LOG_DIR" infra/mosquitto/data

info() { printf '\033[36m[info]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$1"; }
fail() { printf '\033[31m[fail]\033[0m %s\n' "$1"; exit 1; }

# ---------- 0. Mosquitto 凭证（Broker 已禁用匿名，必须先于 Broker 启动） ----------
CRED_PASSWD="$ROOT/infra/mosquitto/passwd"
CRED_STATE="$RUN_DIR/mqtt/credentials.json"
if [ ! -f "$CRED_PASSWD" ] || [ ! -f "$CRED_STATE" ]; then
  info "生成 Mosquitto 服务/客户端/种子设备凭证（不入库）"
  "$PYTHON_BIN" scripts/mqtt_credentials.py init \
    --device-id dev-0001 --device-id dev-0002 \
    --device-id dev-0003 --device-id dev-0004 >/dev/null
fi
[ -f "$CRED_STATE" ] || fail "缺少 $CRED_STATE，无法向网关下发 Broker 凭证"

eval "$("$PYTHON_BIN" - "$CRED_STATE" <<'PY'
import json, shlex, sys
state = json.load(open(sys.argv[1]))
for key, var in (
    ("iot-gateway", "MQTT_PASSWORD"),
    ("web-console", "MQTT_WEB_PASSWORD"),
    ("mobile-console", "MQTT_MOBILE_PASSWORD"),
):
    password = state["services"].get(key, {}).get("password")
    if not password:
        raise SystemExit(f"[fail] 状态文件中缺少 {key} 口令，请执行 scripts/mqtt_credentials.py rotate --subject {key} --reload")
    print(f"export {var}={shlex.quote(password)}")
PY
)"
export MQTT_USERNAME=iot-gateway
export MQTT_WEB_USERNAME=web-console
export MQTT_MOBILE_USERNAME=mobile-console
export MQTT_PASSWORD_FILE="$CRED_PASSWD"
export MQTT_RELOAD_COMMAND="kill -HUP \$(cat '$RUN_DIR/mosquitto.pid')"

# ---------- 1. Mosquitto ----------
if lsof -nP -iTCP:1883 -sTCP:LISTEN >/dev/null 2>&1; then
  info "Mosquitto 已在运行（1883 端口被占用）"
else
  [ -x "$MQ_BIN" ] || fail "未找到 mosquitto，请执行 brew install mosquitto"
  info "启动 Mosquitto（1883 MQTT / 9001 WebSocket，匿名已关闭）"
  # 配置使用相对路径，Broker 工作目录必须是 infra/mosquitto/
  pushd infra/mosquitto >/dev/null
  nohup "$MQ_BIN" -c mosquitto.conf >"$LOG_DIR/mosquitto.log" 2>&1 &
  echo $! > "$RUN_DIR/mosquitto.pid"
  popd >/dev/null
  sleep 1
fi

# ---------- 2. PostgreSQL ----------
if "$PG_BIN/pg_isready" -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
  info "PostgreSQL 已在运行"
else
  [ -x "$PG_BIN/pg_ctl" ] || fail "未找到 PostgreSQL，请执行 brew install postgresql@16"
  info "启动 PostgreSQL"
  "$PG_BIN/pg_ctl" -D "$PGDATA" -l "$LOG_DIR/postgres.log" start
  sleep 2
fi

# ---------- 3. 初始化数据库（幂等） ----------
info "初始化数据库角色 / 库 / 表结构"
"$PG_BIN/psql" -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='iot'" | grep -q 1 || \
  "$PG_BIN/psql" -d postgres -c "CREATE ROLE iot LOGIN PASSWORD 'iot_pass';"
"$PG_BIN/psql" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='iot'" | grep -q 1 || \
  "$PG_BIN/createdb" -O iot iot
PGPASSWORD=iot_pass "$PG_BIN/psql" -h 127.0.0.1 -U iot -d iot -f infra/postgres/init.sql >/dev/null

# ---------- 4. 网关虚拟环境 ----------
if [ ! -x gateway/.venv/bin/python ]; then
  # macOS 系统自带 Python 3.9 无 pip/venv 且版本过旧，必须使用 Homebrew Python
  [ -x "$PYTHON_BIN" ] || fail "未找到 $PYTHON_BIN，请执行 brew install python@3.14"
  info "创建网关虚拟环境（$PYTHON_BIN）"
  "$PYTHON_BIN" -m venv gateway/.venv
  gateway/.venv/bin/pip install -q --upgrade pip
  info "安装网关依赖"
  gateway/.venv/bin/pip install -q -r gateway/requirements.txt
fi

if [ ! -f gateway/.env ]; then
  info "生成 gateway/.env（首次启动从 .env.example 复制）"
  cp gateway/.env.example gateway/.env
fi

# ---------- 5. 启动网关 ----------
if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  warn "8000 端口已被占用，网关可能已在运行，跳过启动"
else
  info "启动 Python 网关（http://127.0.0.1:8000/docs）"
  cd gateway
  nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 >>"$LOG_DIR/gateway.log" 2>&1 &
  echo $! > "$RUN_DIR/gateway.pid"
  cd "$ROOT"
  sleep 2
fi

# ---------- 6. 健康检查 ----------
HEALTH=$(curl -s --max-time 5 http://127.0.0.1:8000/api/health || true)
if echo "$HEALTH" | grep -q '"mqttConnected":true'; then
  info "启动完成：Broker 与网关均已就绪"
else
  warn "网关已启动但 MQTT 未连接，请查看 logs/gateway.log"
fi

cat <<'EOF'

下一步：
  1) 启动管理后台：./scripts/start_web.sh
  2) 启动 Flutter 设备端：cd device_app && flutter run
  3) 无手机时可先用模拟器打点（需设备口令）：
     gateway/.venv/bin/python scripts/mqtt_credentials.py show | grep -A4 dev-0001
     MQTT_DEVICE_PASSWORD='<上一步输出的口令>' \
       gateway/.venv/bin/python scripts/simulate_device.py --device-id dev-0001 --count 3
EOF
