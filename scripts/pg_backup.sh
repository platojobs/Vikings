#!/usr/bin/env bash
# PostgreSQL 备份脚本（v1.19.0）。
#
# 特性：
#   - pg_dump 全库导出（--clean --if-exists，便于回滚到该时点）；
#   - gzip 压缩，文件名按时间戳命名（postgres-YYYYmmdd-HHMMSS.sql.gz）；
#   - 只保留最近 N 份（默认 14），更早的自动删除；
#   - 任一步失败（dump 失败、压缩损坏、文件为空）都以非零码退出。
#
# 三种取库方式（默认 compose 生产栈）：
#   ./scripts/pg_backup.sh                        # compose 生产栈（推荐 cron 使用）
#   ./scripts/pg_backup.sh --container <name>     # 任意已运行容器（演练/临时库）
#   ./scripts/pg_backup.sh --dsn <postgres-url>   # 本机 pg_dump 直连（需安装客户端）
#
# 常用参数：
#   --dir <目录>        备份目录（默认 backups/postgres，可用 BACKUP_DIR 覆盖）
#   --keep <N>          保留份数（默认 14，可用 BACKUP_KEEP 覆盖）
#   --db-user/--db-name container/dsn 模式下的库账号与库名
#   BACKUP_METRICS_FILE 可选；写入 node_exporter 文本指标，供备份失败告警
#
# 示例（生产每日 02:00）：
#   0 2 * * * cd /opt/vikings && ./scripts/pg_backup.sh --keep 30 >> logs/backup.log 2>&1
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="infra/docker-compose.prod.yml"
ENV_FILE="infra/prod/.env"

MODE="compose"
CONTAINER=""
DSN=""
DB_USER="${POSTGRES_USER:-iot}"
DB_NAME="${POSTGRES_DB:-iot}"
OUT_DIR="${BACKUP_DIR:-$ROOT_DIR/backups/postgres}"
KEEP="${BACKUP_KEEP:-14}"
# 可选：写入 node_exporter textfile collector 指标，供 Prometheus 告警备份失败。
# 例：BACKUP_METRICS_FILE=/var/lib/node_exporter/textfile/iot-backup.prom
METRICS_FILE="${BACKUP_METRICS_FILE:-}"

log() { printf '[pg-backup] %s\n' "$*"; }

write_metrics() {
  [ -n "$METRICS_FILE" ] || return 0
  mkdir -p "$(dirname "$METRICS_FILE")" 2>/dev/null || return 0
  {
    echo "# HELP iot_backup_last_success_timestamp_seconds 最近一次成功备份的 Unix 时间戳。"
    echo "# TYPE iot_backup_last_success_timestamp_seconds gauge"
    echo "iot_backup_last_success_timestamp_seconds ${1:-0}"
    echo "# HELP iot_backup_last_status 最近一次备份结果（1=成功，0=失败）。"
    echo "# TYPE iot_backup_last_status gauge"
    echo "iot_backup_last_status ${2:-0}"
  } > "${METRICS_FILE}.part" 2>/dev/null && mv "${METRICS_FILE}.part" "$METRICS_FILE" || true
}

fail() {
  write_metrics 0 0
  printf '[pg-backup][fail] %s\n' "$*" >&2
  exit 1
}

usage() {
  sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --compose) MODE="compose"; shift ;;
    --container) MODE="container"; CONTAINER="${2:?}"; shift 2 ;;
    --dsn) MODE="dsn"; DSN="${2:?}"; shift 2 ;;
    --db-user) DB_USER="${2:?}"; shift 2 ;;
    --db-name) DB_NAME="${2:?}"; shift 2 ;;
    --dir) OUT_DIR="${2:?}"; shift 2 ;;
    --keep) KEEP="${2:?}"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) fail "未知参数：${1}（使用 --help 查看用法）" ;;
  esac
done

[ "$KEEP" -ge 1 ] 2>/dev/null || fail "--keep 必须是正整数"
mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$OUT_DIR/postgres-$STAMP.sql.gz"
TMP="$TARGET.part"
trap 'rm -f "$TMP"' EXIT

dump() {
  case "$MODE" in
    compose)
      [ -f "$ENV_FILE" ] || fail "缺少 ${ENV_FILE}；请先执行 scripts/prod_up.sh 或改用 --container/--dsn"
      docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T postgres \
        sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges'
      ;;
    container)
      docker exec -i "$CONTAINER" \
        sh -c "pg_dump -U '$DB_USER' -d '$DB_NAME' --clean --if-exists --no-owner --no-privileges"
      ;;
    dsn)
      pg_dump "$DSN" --clean --if-exists --no-owner --no-privileges
      ;;
  esac
}

log "开始备份（模式=${MODE}，目标=${OUT_DIR}）"
case "$MODE" in
  compose) log "库：postgres 容器内 \$POSTGRES_DB" ;;
  container) log "库：${CONTAINER} / ${DB_NAME}（用户 ${DB_USER}）" ;;
  dsn) log "库：由 DSN 指定" ;;
esac

# pipefail 保证 pg_dump 失败不会被 gzip 的成功掩盖
dump | gzip -9 > "$TMP" || fail "pg_dump/gzip 失败，未生成可用备份"
gzip -t "$TMP" || fail "压缩文件校验失败：$TMP"
[ -s "$TMP" ] || fail "备份文件为空（库不存在或无权限？）"

mv "$TMP" "$TARGET"
chmod 600 "$TARGET"
trap - EXIT

SIZE="$(du -h "$TARGET" | cut -f1)"
log "备份完成：${TARGET}（${SIZE}）"
write_metrics "$(date +%s)" 1

# 仅保留最近 N 份；按 mtime 倒序，命名也含时间戳，二者一致
count="$(ls -1 "$OUT_DIR"/postgres-*.sql.gz 2>/dev/null | wc -l | tr -d ' ')"
if [ "$count" -gt "$KEEP" ]; then
  ls -1t "$OUT_DIR"/postgres-*.sql.gz | tail -n +"$((KEEP + 1))" | while read -r old; do
    rm -f "$old"
    log "清理旧备份（保留 ${KEEP} 份）：${old}"
  done
fi

log "成功。恢复命令：./scripts/pg_restore.sh --file ${TARGET}"
