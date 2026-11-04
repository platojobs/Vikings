#!/usr/bin/env bash
# PostgreSQL 恢复脚本（v1.19.0）。
#
# 行为：把备份文件恢复成「备份时点的数据库状态」。
#   - 备份使用 pg_dump --clean --if-exists 生成，恢复会 DROP 目标库中现有表后重建；
#   - 不使用 --yes 时必须手工输入库名确认，防止误连生产库；
#   - 建议先停网关停止写入（--stop-gateway 可自动执行，仅 compose 模式）；
#   - 任一步失败以非零码退出。
#
# 用法：
#   ./scripts/pg_restore.sh --file backups/postgres/postgres-<时间>.sql.gz
#   ./scripts/pg_restore.sh --file <file> --yes            # 自动化/演练
#   ./scripts/pg_restore.sh --file <file> --container <name> --db-user iot --db-name iot --yes
#   ./scripts/pg_restore.sh --file <file> --dsn <postgres-url> --yes
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="infra/docker-compose.prod.yml"
ENV_FILE="infra/prod/.env"

MODE="compose"
CONTAINER=""
DSN=""
DB_USER="${POSTGRES_USER:-iot}"
DB_NAME="${POSTGRES_DB:-iot}"
FILE=""
ASSUME_YES=0
STOP_GATEWAY=0

log() { printf '[pg-restore] %s\n' "$*"; }
fail() { printf '[pg-restore][fail] %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --file) FILE="${2:?}"; shift 2 ;;
    --compose) MODE="compose"; shift ;;
    --container) MODE="container"; CONTAINER="${2:?}"; shift 2 ;;
    --dsn) MODE="dsn"; DSN="${2:?}"; shift 2 ;;
    --db-user) DB_USER="${2:?}"; shift 2 ;;
    --db-name) DB_NAME="${2:?}"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --stop-gateway) STOP_GATEWAY=1; shift ;;
    -h|--help) usage 0 ;;
    *) fail "未知参数：${1}（使用 --help 查看用法）" ;;
  esac
done

[ -n "$FILE" ] || fail "缺少 --file <备份文件>"
[ -f "$FILE" ] || fail "备份文件不存在：$FILE"
gzip -t "$FILE" || fail "备份文件损坏（gzip 校验失败）：$FILE"

case "$MODE" in
  compose) TARGET_DESC="生产栈 postgres（库名由容器内 \$POSTGRES_DB 决定）" ;;
  container) TARGET_DESC="容器 ${CONTAINER} 的数据库 ${DB_NAME}（用户 ${DB_USER}）" ;;
  dsn) TARGET_DESC="DSN 指定的数据库" ;;
esac

cat <<EOF
[pg-restore] 即将恢复：
  备份文件 : $FILE
  目标     : $TARGET_DESC
  影响     : 目标库现有表会被 DROP 并重建为备份时点状态（备份之后写入的数据会丢失）
  建议     : 先停网关停止写入；恢复完成并校验后再启动
EOF

if [ "$ASSUME_YES" -ne 1 ]; then
  if [ "$MODE" = "compose" ] && [ -f "$ENV_FILE" ]; then
    expected="$(grep -E '^POSTGRES_DB=' "$ENV_FILE" | cut -d= -f2-)"
    [ -n "$expected" ] && DB_NAME="$expected"
  fi
  printf '[pg-restore] 输入目标库名确认继续（默认 %s）: ' "$DB_NAME"
  read -r answer
  [ "$answer" = "$DB_NAME" ] || fail "确认不一致（输入 '${answer}'，期望 '${DB_NAME}'），已取消"
else
  log "--yes 已指定：跳过交互确认"
fi

if [ "$STOP_GATEWAY" -eq 1 ]; then
  [ "$MODE" = "compose" ] || fail "--stop-gateway 仅适用于 compose 模式"
  log "停止网关写入：gateway"
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" stop gateway
fi

restore() {
  case "$MODE" in
    compose)
      gunzip -c "$FILE" | docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T postgres \
        sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
      ;;
    container)
      gunzip -c "$FILE" | docker exec -i "$CONTAINER" \
        psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME"
      ;;
    dsn)
      gunzip -c "$FILE" | psql "$DSN" -v ON_ERROR_STOP=1
      ;;
  esac
}

restore || fail "恢复失败（psql 返回非 0），目标库可能处于中间状态，请保留现场并重试"
log "恢复完成"

# 校验迁移版本，确认恢复后 schema 与备份时点一致
case "$MODE" in
  compose)
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T postgres \
      sh -c 'psql -tAc "select version_num from alembic_version" -U "$POSTGRES_USER" -d "$POSTGRES_DB" 2>/dev/null || echo "(无 alembic_version)"'
    ;;
  container)
    docker exec -i "$CONTAINER" \
      psql -tAc "select version_num from alembic_version" -U "$DB_USER" -d "$DB_NAME" 2>/dev/null \
      || echo "(无 alembic_version)"
    ;;
  dsn)
    psql "$DSN" -tAc "select version_num from alembic_version" 2>/dev/null || echo "(无 alembic_version)"
    ;;
esac

cat <<EOF
[pg-restore] 下一步：
  1) 启动网关（若已停）：docker compose -f $COMPOSE_FILE --env-file $ENV_FILE start gateway
  2) 验证健康与数据：curl -k https://<host>/api/health
  3) 抽查设备/最新上报/告警数量是否与备份时点一致
EOF
