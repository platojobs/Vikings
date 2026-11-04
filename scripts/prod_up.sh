#!/usr/bin/env bash
# 一键拉起生产栈（v1.18.0）。
#
# 在干净机器上执行：
#   ./scripts/prod_up.sh
#
# 它会：
#   1. 检查 Docker / Compose 可用；
#   2. 首次运行时由 infra/prod/.env.example 生成 infra/prod/.env，并填入随机密钥；
#   3. 生成 Mosquitto 服务/客户端/（可选）设备凭证到 infra/prod/secrets/；
#   4. 生成自签 HTTPS 证书（已存在则跳过）；
#   5. docker compose 构建并启动，等待全部健康检查通过。
#
# 可选环境变量：
#   SEED_DEVICE_IDS=dev-0001,dev-0002   首次生成时顺便签发设备凭证（默认空）
#   PUBLIC_ORIGIN=https://iot.example.com  覆盖公开访问源（也决定自签证书域名）
#   PROD_SKIP_BUILD=1                   仅重启，不重新构建镜像
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE_FILE="infra/docker-compose.prod.yml"
ENV_FILE="infra/prod/.env"
ENV_TEMPLATE="infra/prod/.env.example"
SECRETS_DIR="infra/prod/secrets"
PASSWORD_FILE="$SECRETS_DIR/mosquitto.passwd"
STATE_FILE=".run/mqtt/prod-credentials.json"
CERT_DIR="infra/prod/nginx/certs"

log() { printf '[prod-up] %s\n' "$*"; }
fail() { printf '[prod-up][fail] %s\n' "$*" >&2; exit 1; }

# ---------- 1. 前置检查 ----------
command -v docker >/dev/null 2>&1 || fail "未找到 docker，请先安装 Docker Engine / Docker Desktop / colima"
docker info >/dev/null 2>&1 || fail "Docker 守护进程不可用；macOS 可执行 colima start，或启动 Docker Desktop"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 不可用（需要 docker compose 子命令）"
command -v openssl >/dev/null 2>&1 || fail "未找到 openssl，无法生成本地自签证书"

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  if [ -x "gateway/.venv/bin/python" ]; then
    PYTHON_BIN="gateway/.venv/bin/python"
  else
    PYTHON_BIN="$(command -v python3 || true)"
  fi
fi
[ -n "$PYTHON_BIN" ] || fail "未找到 python3，无法生成密钥与 MQTT 凭证"

if [ "$(id -u)" = "0" ]; then
  fail "请用非 root 部署用户运行：网关容器会以 DEPLOY_UID 运行，root 部署会破坏非 root 约束"
fi

mkdir -p "$SECRETS_DIR" "$CERT_DIR" "infra/prod/nginx/acme" "$(dirname "$STATE_FILE")"

# ---------- 2. 首次生成 .env ----------
if [ ! -f "$ENV_FILE" ]; then
  log "未发现 ${ENV_FILE}，按模板生成随机密钥"
  cp "$ENV_TEMPLATE" "$ENV_FILE"
  "$PYTHON_BIN" - "$ENV_FILE" <<'PY'
import os
import secrets
import sys
from pathlib import Path

path = Path(sys.argv[1])
values = {
    "JWT_SECRET": secrets.token_hex(48),
    "POSTGRES_PASSWORD": secrets.token_hex(24),
    "ADMIN_PASSWORD": secrets.token_hex(16),
    "DEPLOY_UID": str(os.getuid()),
    "DEPLOY_GID": str(os.getgid()),
}
public_origin = os.environ.get("PUBLIC_ORIGIN", "").strip()
if public_origin:
    values["PUBLIC_ORIGIN"] = public_origin
for key in ("ADMIN_USERNAME", "ADMIN_PASSWORD"):
    override = os.environ.get(key, "").strip()
    if override:
        values[key] = override
# 本机验证时可通过环境变量改端口（生产模板默认 80/443/1883）
for key in ("HTTP_PORT", "HTTPS_PORT", "MQTT_PORT"):
    override = os.environ.get(key, "").strip()
    if override:
        values[key] = override

lines = path.read_text(encoding="utf-8").splitlines()
out = []
seen = set()
for line in lines:
    key, sep, _ = line.partition("=")
    if sep and key in values:
        out.append(f"{key}={values[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in values.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
  # PUBLIC_ORIGIN 可能被脚本覆盖，单独同步给后续证书生成
else
  log "复用已有 ${ENV_FILE}（不会覆盖人工修改）"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
fi

# ---------- 3. MQTT 凭证 ----------
log "初始化 Mosquitto 服务/客户端凭证"
INIT_ARGS=(init)
if [ -n "${SEED_DEVICE_IDS:-}" ]; then
  IFS=',' read -r -a _devices <<<"${SEED_DEVICE_IDS}"
  for device_id in "${_devices[@]}"; do
    [ -n "$device_id" ] && INIT_ARGS+=(--device-id "$device_id")
  done
fi
MQTT_PASSWORD_FILE_MODE=644 "$PYTHON_BIN" scripts/mqtt_credentials.py \
  --password-file "$PASSWORD_FILE" \
  --state-file "$STATE_FILE" \
  "${INIT_ARGS[@]}" >/dev/null

# 把服务口令回填到 .env（明文只存在于 gitignored 的 .env 与 0600 状态文件）
"$PYTHON_BIN" - "$ENV_FILE" "$STATE_FILE" <<'PY'
import json
import os
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
state = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
services = state.get("services", {})
mapping = {
    "iot-gateway": "MQTT_PASSWORD",
    "web-console": "MQTT_WEB_PASSWORD",
    "mobile-console": "MQTT_MOBILE_PASSWORD",
}
updates = {}
for username, env_key in mapping.items():
    entry = services.get(username)
    if not entry or not entry.get("password"):
        raise SystemExit(
            f"缺少 {username} 的口令记录：{sys.argv[2]}。\n"
            f"若 passwd 已存在但状态文件丢失，请先轮换该身份：\n"
            f"  python scripts/mqtt_credentials.py rotate --subject service:{username} "
            f"--password-file infra/prod/secrets/mosquitto.passwd --state-file {sys.argv[2]}"
        )
    updates[env_key] = entry["password"]

lines = env_path.read_text(encoding="utf-8").splitlines()
out = []
seen = set()
for line in lines:
    key, sep, _ = line.partition("=")
    if sep and key in updates:
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
os.chmod(env_path, 0o600)
PY

# Broker 与网关以不同 uid 运行：passwd 必须可读（仅含 hash）
chmod 644 "$PASSWORD_FILE"
chmod 600 "$ENV_FILE"

# ---------- 4. HTTPS 证书 ----------
CERT_DOMAIN="$("$PYTHON_BIN" - "$ENV_FILE" <<'PY'
import sys
from pathlib import Path
from urllib.parse import urlsplit

for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if line.startswith("PUBLIC_ORIGIN="):
        host = urlsplit(line.split("=", 1)[1].strip()).hostname
        print(host or "localhost")
        break
else:
    print("localhost")
PY
)"
if [ ! -f "$CERT_DIR/fullchain.pem" ] || [ ! -f "$CERT_DIR/privkey.pem" ]; then
  log "生成自签证书（CN=${CERT_DOMAIN}）；生产请替换为正式证书，见 docs/deployment.md"
  bash scripts/gen_self_signed_cert.sh "$CERT_DIR" "$CERT_DOMAIN"
else
  log "复用已有 HTTPS 证书：$CERT_DIR"
fi

# ---------- 5. 构建并启动 ----------
COMPOSE=(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE")

if [ "${PROD_SKIP_BUILD:-0}" = "1" ]; then
  log "跳过镜像构建（PROD_SKIP_BUILD=1）"
  "${COMPOSE[@]}" up -d
else
  log "构建镜像并启动全栈（首次构建耗时较长）"
  "${COMPOSE[@]}" up -d --build
fi

log "等待健康检查通过（最多 180 秒）"
healthy=0
for _ in $(seq 1 60); do
  all_healthy=1
  for service in postgres mosquitto gateway web proxy; do
    container_id="$("${COMPOSE[@]}" ps -q "$service" 2>/dev/null || true)"
    if [ -z "$container_id" ]; then
      all_healthy=0
      break
    fi
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}running{{end}}' "$container_id" 2>/dev/null || echo unknown)"
    if [ "$status" != "healthy" ] && [ "$status" != "running" ]; then
      all_healthy=0
      break
    fi
  done
  if [ "$all_healthy" = "1" ]; then
    healthy=1
    break
  fi
  sleep 3
done

"${COMPOSE[@]}" ps

if [ "$healthy" != "1" ]; then
  fail "健康检查未在 180 秒内全部通过；查看日志：docker compose -f ${COMPOSE_FILE} --env-file ${ENV_FILE} logs --tail=80"
fi

log "全部服务健康。访问入口："
log "  管理后台   : https://${CERT_DOMAIN}/（自签证书首次需信任）"
log "  REST 健康  : https://${CERT_DOMAIN}/api/health"
log "  设备 MQTT  : ${CERT_DOMAIN}:$(grep -E '^MQTT_PORT=' "$ENV_FILE" | cut -d= -f2)"
log "管理员账号/口令在 ${ENV_FILE}（gitignored，权限 600）"
log "停止：docker compose -f $COMPOSE_FILE --env-file $ENV_FILE down"
