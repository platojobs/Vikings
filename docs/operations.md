# 运维手册（v1.21.0）

> 面向已按 [deployment.md](deployment.md) 完成部署的运维人员。
> 所有命令均在仓库根目录执行，并使用生产编排文件。

```bash
COMPOSE="docker compose -f infra/docker-compose.prod.yml --env-file infra/prod/.env"
```

建议在 shell 里先定义 `COMPOSE`（部分旧版 compose 不支持变量展开到命令位置时，直接复制完整命令）。

---

## 1. 日常启停与状态

```bash
# 启动（不重建镜像）
$COMPOSE up -d

# 查看状态（含健康检查）
$COMPOSE ps

# 停止（保留数据卷与配置）
$COMPOSE down

# 停止并删除数据卷（⚠️ 会丢失数据库与 Broker retained 数据）
# $COMPOSE down -v
```

- 全部常驻服务 `restart: unless-stopped`；服务器重启后会随 Docker 自启。
- `migrate` 是一次性服务：正常情况下显示 `Exited (0)`，表示 schema 已到当前版本。
- 健康状态定义：`docker inspect -f '{{.State.Health.Status}}' <container>`。
- 修改 `.env` 后需要重建相关容器：`$COMPOSE up -d gateway`（仅改 gateway 环境变量时）。

---

## 2. 日志、健康检查与指标

```bash
# 跟踪全部
$COMPOSE logs -f --tail=100

# 单服务
$COMPOSE logs -f --tail=100 gateway
$COMPOSE logs -f --tail=100 mosquitto
$COMPOSE logs -f --tail=100 proxy

# 查看本次启动的迁移结果
$COMPOSE logs migrate

# 最近一次启动失败的容器日志
$COMPOSE ps -a
docker logs --tail=200 <container>
```

排障顺序：`postgres` → `migrate` → `mosquitto` → `gateway` → `web` → `proxy`。

### 2.1 结构化日志

生产默认 `LOG_FORMAT=json`（见 `infra/prod/.env`），每行一条 JSON；本地可用 `LOG_FORMAT=text`。
请求日志字段：`time/level/logger/message/requestId/event/method/path/status/durationMs/clientIp/user/error`。
**不记录**请求体、请求头、查询串与 Cookie；`Authorization`、Bearer token、口令/密钥键值、
数据库 URL 中的口令均替换为 `[REDACTED]`（异常堆栈同样脱敏）。

```bash
# 只看 5xx
docker compose -f infra/docker-compose.prod.yml logs gateway --since 30m | \
  jq -c 'select(.status >= 500)'

# 按 requestId 定位一次报错
docker compose -f infra/docker-compose.prod.yml logs gateway --since 1h | \
  jq -c 'select(.requestId=="<用户提供的 requestId>")'
```

### 2.2 请求 ID 追踪

网关接受上游 `X-Request-Id`（反代会透传），没有则生成 32 位 ID；响应头始终回写，
错误响应体带 `requestId`。用户报错时先要这个 ID，再用上面的 `jq` 命令检索，
最后用反代日志里的 `grid=` 字段对齐 nginx 访问日志。

### 2.3 健康检查分层

| 端点 | 语义 | 是否查库/查 Broker |
| --- | --- | --- |
| `/api/health` | 存活：进程活着就 200 | 否 |
| `/api/health/ready` | 就绪：DB 真实读 `device` 表 + MQTT 已连接 | 是 |

容器 `HEALTHCHECK` 与编排健康检查引用 `/ready`：数据库或 Broker 不可用时容器不应标记为健康。
数据库故障时 `/api/health` 仍返回 200，`/ready` 返回 503 并在 `components` 指出具体层。

### 2.4 指标与告警

Prometheus 端点 `GET /metrics`（经反代只允许内网来源，公网 403）。关键指标：

- Broker：`iot_mqtt_connected`、`iot_mqtt_disconnects_total`、`iot_mqtt_connect_failures_total`；
- 数据库：`iot_db_ready`、`iot_db_query_errors_total`、`iot_db_pool_checked_out`；
- 网关：`iot_http_requests_total{status}`、`iot_http_request_duration_seconds`；
- 登录：`iot_login_failures_total`、`iot_login_rate_limited_total`；
- 备份：`iot_backup_last_status`、`iot_backup_last_success_timestamp_seconds`。

告警阈值建议与可直接加载的规则见
[observability.md](observability.md#5-告警口径与阈值) 与
[infra/prometheus/iot-alerts.yml](../infra/prometheus/iot-alerts.yml)。
完整字段说明、采集配置与故障定位矩阵见 [observability.md](observability.md)。

常见现象见 [deployment.md](deployment.md#9-常见故障排查)。

---

## 3. 凭证轮换

凭证状态文件（仅本地、0600、gitignored）：

| 用途 | passwd | 状态文件（含明文口令） |
| --- | --- | --- |
| 生产 | `infra/prod/secrets/mosquitto.passwd` | `.run/mqtt/prod-credentials.json` |
| 本地开发 | `infra/mosquitto/passwd` | `.run/mqtt/credentials.json` |

> **Docker 生产环境下 Broker 重载**：轮换命令执行后必须重启 Broker 才会使旧口令失效：
>
> ```bash
> $COMPOSE restart mosquitto
> ```
>
> API `POST /api/devices/{id}/mqtt-credential` 会写 passwd、返回 `reloaded=false`；
> 随后同样需要重启 Broker。宿主机 CLI 轮换可用 `--reload-command` 指定自定义命令，
> 但不要试图从网关容器内操作 Docker。

### 3.1 设备凭证轮换

#### 3.1.1 在界面轮换（v1.26.0，推荐）

1. 以 **admin** 登录 Web 后台，打开「设备」→ 点击设备行进入详情抽屉；
2. 「MQTT 凭证」区可看到：是否签发、版本、最近轮换时间、是否吊销、指纹前 8 位
   （指纹前缀只用于人工对账，界面不会显示口令或完整指纹）；
3. 点击「轮换凭证」，在二次确认框核对影响后确认：
   - 旧凭证对新连接立即失效；已建立的连接可能维持到下次断线重连，因此设备可能短暂仍显示在线；
   - 设备必须更新配置中的口令，否则断线后无法重连；
   - 若 Broker 未重载，新口令已写入但旧凭证可能仍有效。
4. 确认后弹出**一次性口令**：只有这一次可见。先点「复制口令」，写入设备安全存储或密钥管理，
   再点「我已保存，关闭」。关闭后界面与服务端都无法再查到该口令（服务端只保存指纹），
   需要新口令只能再次轮换。
5. 让设备用新口令重连，确认设备状态恢复为在线。

移动端设备详情抽屉提供同样的状态展示与轮换流程（轮换/吊销按钮仅 admin 可见）。

#### 3.1.2 命令行轮换（等价入口）

```bash
python3 scripts/mqtt_credentials.py rotate \
  --subject device:dev-0001 \
  --password-file infra/prod/secrets/mosquitto.passwd \
  --state-file .run/mqtt/prod-credentials.json

$COMPOSE restart mosquitto
```

- 新口令只在命令输出中出现一次，立即写入设备安全存储；
- 旧口令在 Broker 重载后对新连接失效（已建立的连接不会被强制断开）；
- 设备端断线重连时旧凭证会收到 CONNACK rc=5/135。

#### 3.1.3 轮换后提示「Broker 未重载」怎么办

界面/接口出现 `reloaded=false`（或提示「已写入新凭证，但 Broker 未重载，旧凭证可能仍然有效」）
说明设备元数据与 passwd 已更新，但 Broker 进程仍在用旧密码文件。此时**不要**把这次轮换当作成功：

1. 宿主机执行重载：`systemctl reload mosquitto`，或 `$COMPOSE restart mosquitto`；
2. 查看 `reload_detail` 与 Broker 日志，确认重载成功（无配置错误、进程未退出）；
3. 用新口令做一次连接验证；旧口令应被拒绝（rc=5/135）；
4. 若重载持续失败，按 3.2 的吊销流程先下线旧账号，再修复 Broker 后重新签发；
5. 审计 `device.credential_issue` 的 `result=reload_failed` 可作为排查与复盘依据。

### 3.2 吊销设备凭证

界面入口（v1.26.0，仅 admin）：设备详情 → 「MQTT 凭证」→「吊销凭证」→ 二次确认。
吊销后设备详情显示「已吊销」，设备必须「重新签发」才能接入；`reloaded=false` 时同样按
3.1.3 先重载 Broker，否则旧账号可能仍能连接。

```bash
python3 scripts/mqtt_credentials.py revoke \
  --subject device:dev-0001 \
  --password-file infra/prod/secrets/mosquitto.passwd \
  --state-file .run/mqtt/prod-credentials.json

$COMPOSE restart mosquitto
```

### 3.3 客户端（Web / 移动端）只读凭证轮换

`web-console` / `mobile-console` 是服务身份：口令由网关环境变量下发，Broker 只读 ACL 不变。

```bash
# 1) 轮换其中一个身份
python3 scripts/mqtt_credentials.py rotate \
  --subject service:web-console \
  --password-file infra/prod/secrets/mosquitto.passwd \
  --state-file .run/mqtt/prod-credentials.json

# 2) 把新口令写回 infra/prod/.env 的 MQTT_WEB_PASSWORD=...
# 3) 重建网关（让 /api/auth/mqtt-credentials 返回新口令）
$COMPOSE up -d gateway

# 4) 重载 Broker（旧口令失效）
$COMPOSE restart mosquitto

# 5) 已登录的 Web/移动端需重新登录或重新换取凭证
```

> 轮换 `iot-gateway` 服务身份时，必须同步更新 `.env` 的 `MQTT_PASSWORD` 并重建 gateway，
> 否则网关无法连接 Broker，健康检查会失败。

### 3.4 新增设备凭证

```bash
# 方式 A：先入库再调 API（API 会写 passwd，但仍需重启 Broker）
curl -sk -X POST https://<host>/api/devices \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"device_id":"dev-0100","name":"新设备","model":"BMS-100"}'

# 方式 B：宿主机 CLI（适合批量导入/离线签发）
python3 scripts/mqtt_credentials.py issue --subject device:dev-0100 \
  --password-file infra/prod/secrets/mosquitto.passwd \
  --state-file .run/mqtt/prod-credentials.json
$COMPOSE restart mosquitto
```

### 3.5 强制某用户下线（账号会话吊销，v1.24.0）

适用场景：员工离职、账号疑似被盗、需要立即踢掉某账号的全部浏览器/手机会话。

```bash
# 1) 管理员登录拿 access（任何 admin 账号）
TOKEN=$(curl -sk -X POST https://<host>/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<管理员口令>"}' | jq -r '.data.token')

# 2) 吊销目标用户的全部 refresh（返回 {username, revoked:<条数>}）
curl -sk -X POST https://<host>/api/users/alice/sessions/revoke \
  -H "Authorization: Bearer $TOKEN"
```

**生效边界（务必如实告知用户）**：

- `revoked` 条数对应被吊销的 refresh；目标用户下一次刷新即失败，客户端随之回登录页；
- **已签发的 access 仍可用到自身过期**，最长 `ACCESS_TOKEN_TTL_SECONDS`（默认 20 分钟）；
  需要更快生效时，把该值临时调到 300~900 并滚动重启网关（代价是刷新频率上升）；
- 改口令（`PATCH /api/users/{username}`）会自动吊销该用户全部 refresh，并在响应里返回
  `sessionsRevoked` 条数；删除账号同样会吊销；
- 这些操作都写审计：`session.revoke`（管理员吊销）、`logout`（登出）、
  `user.update`（改密/角色变更，含 `sessionsRevoked`），可在「审计日志」页按 target 用户过滤核对；
- 若怀疑 refresh 被重放，网关会自动吊销该用户全部会话并写 `auth.refresh_reuse`，
  无需人工干预，但应排查令牌泄露渠道。

> 反代层无法单独吊销单个用户，请勿用「重启网关」当作下线手段：重启只会清空进程内登录限流计数，
> 对已签发 JWT 没有影响。

---

## 4. 数据库迁移（Alembic）

v1.19.0 起，表结构只由 `gateway/alembic/versions/` 中的迁移文件管理：

- 启动路径不再执行 `Base.metadata.create_all` 或手写 `ALTER TABLE` 补列；
- 生产编排的 `migrate` 一次性服务在 gateway 启动前执行 `alembic upgrade head`，
  失败时 gateway 不会启动；
- 版本记录在 `alembic_version` 表，`0001_baseline` 对应 v1.18.0 及之前的结构，
  `0002_lifecycle_indexes` 为 v1.19.0 的查询索引，
  `0003_alert_ack_metadata` 为 v1.21.0 的告警处置列（`ack_note` / `acked_by` / `acked_at`）。

### 4.1 升级前必做：备份

**任何结构变更前先备份**，否则回滚只能依赖更早的备份：

```bash
./scripts/pg_backup.sh --keep 30
```

### 4.2 常规升级

```bash
# 1) 先备份（见 4.1 / 5.1）
# 2) 更新代码并重建镜像
git pull --ff-only
$COMPOSE up -d --build

# 3) 确认迁移结果（migrate 应已执行并退出 0）
$COMPOSE logs migrate | tail -20
$COMPOSE exec -T postgres sh -c \
  'psql -tAc "select version_num from alembic_version" -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
# 期望：0003_alert_ack_metadata（以仓库 head 为准）

# 4) 健康检查
curl -k https://<host>/api/health
```

若只改代码不改结构，`migrate` 也会执行一次 `upgrade head`，已是 head 时为空操作。

v1.21.0 的 `0003_alert_ack_metadata` 只新增三个可空列，不重建表、不修改既有行：

- 存量 `open` 行天然为空；
- v1.21.0 之前已确认的历史行无法得知真实操作人与时间，保持 NULL，
  页面显示「历史确认（无处置记录）」，**不会用 `created_at` 冒充 `acked_at`**；
- 回退一个版本（`alembic downgrade -1`）会删除这三列，处置备注随之丢失；
  回退前请先备份（见 4.1）。

### 4.3 已有库（v1.18.0 及更早）

baseline 迁移会先探测 `device` 表：

- 表已存在：**不重建、不清空**，只记录 baseline 版本，然后继续执行增量迁移；
- 空库：创建全部表。

自动判定（推荐）：直接 `$COMPOSE up -d`，日志应包含：

```
[baseline] 检测到既有 schema，跳过建表，直接标记为已应用
```

显式判定与标记（适合先人工核对再升级）：

```bash
# ① 确认 8 张核心表已在、但还没有 alembic_version
$COMPOSE exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt"'

# ② 标记 baseline（不执行建表），再升级
$COMPOSE run --rm migrate alembic stamp 0001_baseline
$COMPOSE run --rm migrate alembic upgrade head
```

若 `device` 存在但缺少其余核心表，baseline 会直接报错并拒绝继续（半升级状态需人工核对）。

### 4.4 回退一个迁移

```bash
$COMPOSE run --rm migrate alembic downgrade -1
$COMPOSE run --rm migrate alembic current
```

- `downgrade -1` 只回退最近一步（v1.19.0 为删除新增索引），可再次 `upgrade head` 往返；
- **禁止对生产执行 `alembic downgrade base`**：baseline 的 downgrade 会删除全部业务表；
- 版本回滚（代码 + 结构一起回退）以恢复升级前的 `pg_dump` 为主，见 6. 回滚。

### 4.5 新增迁移（开发）

```bash
cd gateway
# 修改 app/models.py 后生成迁移草稿
.venv/bin/python -m alembic revision --autogenerate -m "描述"
# 人工审查 versions/ 下的草稿，补齐 downgrade，再验证
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic downgrade -1 && .venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic check    # 期望 No new upgrade operations detected
```

要求：加字段/加索引可重入（迁移内先检查存在性），且必须提供可用的 `downgrade`。

---

## 5. 备份与恢复

### 5.1 备份

```bash
# 数据库（推荐，脚本负责压缩/时间命名/保留份数/失败非零退出）
./scripts/pg_backup.sh                 # 默认保留 14 份，写入 backups/postgres/
./scripts/pg_backup.sh --keep 30       # 合同/合规要求更长保留
./scripts/pg_backup.sh --dir /backup/vikings

# 手动等价命令（脚本不可用时）
$COMPOSE exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' \
  | gzip > "backups/postgres-$(date +%F-%H%M).sql.gz"
```

- 文件名格式 `postgres-YYYYmmdd-HHMMSS.sql.gz`，权限 `0600`；
- `--clean --if-exists` 让备份可原地恢复（恢复时会先 DROP 再重建，见 5.2）；
- 备份目录 `backups/` 已在 `.gitignore` 中，禁止提交；
- 保留份数只按最新 N 个文件清理，清理动作会打印被删文件。

生产定时（宿主机 cron，每日 02:00）：

```cron
0 2 * * * cd /opt/vikings && BACKUP_METRICS_FILE=/var/lib/node_exporter/textfile/iot-backup.prom ./scripts/pg_backup.sh --keep 30 >> logs/backup.log 2>&1
```

`BACKUP_METRICS_FILE` 可选：写入 `iot_backup_last_status` 与
`iot_backup_last_success_timestamp_seconds`，供 Prometheus 的备份失败/过期告警使用
（见 [observability.md](observability.md#5-告警口径与阈值)）。

存放与异地：

- 本机备份只防误操作，不防磁盘/主机故障；
- 建议每日把 `backups/postgres/*.sql.gz` 同步到对象存储或另一台主机
  （如 `rclone sync backups/postgres remote:vikings-backups/postgres`），并开启服务端加密；
- 密钥与证书（`infra/prod/.env`、`secrets/`、`certs/`）单独离线加密保存，不与数据库备份放在同一介质；
- 每季度至少做一次恢复演练（5.3）。

### 5.2 恢复

```bash
# 1) 停止写入（脚本不会自动停网关，除非显式 --stop-gateway）
$COMPOSE stop gateway

# 2) 恢复（不带 --yes 时必须手工输入库名确认；会覆盖现有表）
./scripts/pg_restore.sh --file backups/postgres/postgres-<时间>.sql.gz

# 3) 启动并验证
$COMPOSE start gateway
$COMPOSE ps
curl -k https://<host>/api/health
```

脚本会在恢复后打印 `alembic_version`，应等于备份时点的版本。
若备份来自旧版本，恢复后再执行 `$COMPOSE up -d`（migrate 会把结构升到当前 head）。

### 5.3 恢复演练记录（v1.19.0，真实执行）

演练在**隔离容器** `vikings-pg-drill`（PostgreSQL 16，独立端口 55432，临时数据卷）中完成，
未触碰开发库与生产卷：

1. 空库执行 `alembic upgrade head`，写入 20 万告警、5 万审计、5 万时序记录；
2. `pg_backup.sh --container vikings-pg-drill --db-user iot --db-name iot` 生成
   `postgres-20261105-030951.sql.gz`（2.4 MB）；
3. 故意改数据：清空告警、删除一半审计、改名 `dev-1`、新增 `dev-999`；
4. `pg_restore.sh --file ... --yes` 恢复；
5. 校验结果：

```
alerts=200000      （恢复前被清空为 0）
audit=50000        （恢复前被删到 25000）
records=50000
device1_name=设备1  （恢复前为“被篡改的设备”）
dev999_exists=0    （备份后新增的设备被回滚）
schema_version=0002_lifecycle_indexes
```

同时验证了：备份失败（容器不存在）退出码 1；恢复确认输入不匹配退出码 1、不会写入；
`--keep 2` 会清理 2 个更旧备份。

### 5.4 Broker 与静态资源配置备份

```bash
# Broker 持久化数据（retained 状态、离线队列）
docker run --rm -v iot-prod_mosquitto-data:/data -v "$PWD/backups":/backup alpine \
  tar czf "/backup/mosquitto-$(date +%F-%H%M).tar.gz" -C /data .

# 密钥与配置（离线加密保存，禁止入库）
tar czf "backups/secrets-$(date +%F).tar.gz" \
  infra/prod/.env infra/prod/secrets infra/prod/nginx/certs
```

---

## 6. 回滚

```bash
# A. 仅代码回滚（数据库结构兼容）
git fetch --tags
git checkout <上一个稳定 tag>       # 例如 v1.18.0
./scripts/prod_up.sh                # 重新构建并滚动替换

# B. 结构也要回退：先恢复升级前的备份，再启动旧版本
$COMPOSE stop gateway
./scripts/pg_restore.sh --file backups/postgres/postgres-<升级前时间>.sql.gz --stop-gateway
git checkout <上一个稳定 tag>
./scripts/prod_up.sh
```

- 回滚前务必确认有升级前备份；结构回滚优先用备份，不要 `alembic downgrade base`；
- 不要用 `docker compose down -v` 清空数据。

---

## 7. 证书续期

```bash
# Let's Encrypt 续期（宿主机）
sudo certbot renew --webroot -w "$PWD/infra/prod/nginx/acme"
sudo cp /etc/letsencrypt/live/<domain>/fullchain.pem infra/prod/nginx/certs/
sudo cp /etc/letsencrypt/live/<domain>/privkey.pem   infra/prod/nginx/certs/
$COMPOSE restart proxy
```

建议把续期放进 cron，并在续期后触发 `restart proxy`；或用外部负载均衡统一终止 TLS。

---

## 8. 数据保留与维护

保留策略（v1.19.0）由网关定时清理任务执行，默认每小时一次（`DATA_PURGE_INTERVAL_SECONDS=3600`）：

| 数据 | 默认保留 | 清理口径 | 配置项 |
| --- | --- | --- | --- |
| `sensor_record` | 90 天 | 按上报时间 `ts` 删除 | `DATA_RETENTION_DAYS` |
| `device_alert` | 180 天 | **只删已确认（acked）**；`open` 永不自动删除 | `ALERT_RETENTION_DAYS` |
| `audit_log` | 365 天 | 按 `created_at` 删除 | `AUDIT_RETENTION_DAYS` |

默认值理由：90 天覆盖设备历史曲线与排障；180 天覆盖两次季度复盘；审计日志是操作追溯依据、
不可重建，因此单独保留一年且比告警更长。

手动触发（与定时任务同一实现）：

```bash
# 预检：只统计将被删除的行数
$COMPOSE exec -T gateway python -m app.maintenance purge --dry-run

# 立即清理
$COMPOSE exec -T gateway python -m app.maintenance purge
# 输出示例：已清理：{"records": 2, "alerts": 3, "audit_logs": 2}
```

维护建议：

- 定期检查磁盘：`docker system df`、`df -h`；
- 若审计日志需长期留存，在清理前导出（`pg_dump` 或定期 `COPY ... TO` 归档），再调低 `AUDIT_RETENTION_DAYS`；
- 告警/审计/告警列表的查询索引由 `0002_lifecycle_indexes` 维护，新增筛选条件时同步补索引并给出 `EXPLAIN` 证据；
- 日志已按容器轮转（10 MB × 3）；如需集中日志，接入 Loki/ELK 等（P1-1）。

---

## 9. 安全例行检查

```bash
# 确认数据库未暴露宿主机端口（应无输出）
$COMPOSE ps | grep -E '5432|8000|9001|8080' || true

# 匿名连接应被拒绝（期望 rc=5 / 135）
python3 - <<'PY'
import paho.mqtt.client as mqtt
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="anon-audit")
rc, _ = c.connect("127.0.0.1", 1883, 5)
print("anonymous rc =", rc)
PY

# 安全头
curl -skI https://<host>/ | grep -Ei 'strict-transport|x-content-type|referrer-policy|content-security'
```

周期性轮换：管理员口令、`JWT_SECRET`（会使现有 token 失效）、MQTT 服务与客户端口令。
轮换 `JWT_SECRET` 后重建 gateway 并通知用户重新登录。

---

## 10. 故障场景处置（v1.23.0）

以下场景均在 E2E（`scripts/e2e/e2e_acceptance.py`）中真实注入并断言，不是纸面推演。
每条给出「语义」与「处置动作」，避免只确认"进程没崩"。

### 10.1 网关在上报 / OTA 过程中重启

**语义**：网关使用 MQTT 非持久会话（clean session），停机期间设备发布到
`cmd/ack`、`sensor/report` 的报文**明确丢弃**，不会在恢复后补投；
已写入数据库的进度（`device_command.state/progress`、`ota_task` 汇总）持久保留。

**处置**：

1. `curl -sf http://127.0.0.1:8000/api/health/ready | jq` 确认 DB 与 MQTT 均 ready；
2. `GET /api/ota/tasks/{taskId}` 对比重启前进度：进度不前进属正常，**不应回退**；
3. 让设备侧重新上报一次终态（设备通常会在重连后补发）；若任务已进入 `failed`
   （设备失败或超时），不要改库复活，直接创建新任务重试；
4. 用审计（`action=ota.create`）与 `.run/e2e/gateway-restart.log` 对齐时间线。

**不要做**：手工把 `success` 写进 `device_command`/`ota_task`。终态不可回退是刻意设计，
绕过它会让后续真实回执失去可比性。

### 10.2 设备自身断连后重连（Broker 未停机）

**语义**：设备主动发布 `offline(reason=sleep)` 或异常掉线触发 LWT 后，网关设备状态转
`offline`；设备用同一凭证重连并发布 `online` 后恢复可用，重连后新下发的指令仍能到达并回执。

**处置**：

1. `GET /api/devices/{deviceId}` 看 `status` 与 `last_seen`；
2. 检查 Broker 日志中该 `clientId` 的 CONNECT/DISCONNECT；若反复重连，优先查弱网、
   clientId 冲突与凭证是否被吊销；
3. 设备长时间不回来时按离线巡检处理（默认 300s 置离线），不要手工改状态。

### 10.3 数据库短暂不可用

**语义**：`/api/health` 保持 200（只看进程存活）；`/api/health/ready` 返回 503，
`components.database.ok=false` 且带脱敏错误摘要；网关进程不退出，后台巡检/清理
下个周期自动重试。DB 恢复后 `/ready` 回 200，REST 继续服务。

**处置**：

1. `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/health`（应为 200）；
2. `curl -s http://127.0.0.1:8000/api/health/ready | jq '.data.components.database'`；
3. 按错误摘要检查：磁盘写满、SQLite 文件权限/独占锁、Postgres 连接数或容器健康；
4. 恢复后再次确认 `/ready=200` 与 `/api/devices` 可读，再决定是否需要补偿丢失的上报。

> `/ready` 从 v1.23.0 起真实读取 `device` 表，而不是 `SELECT 1`，因此独占锁/存储损坏
> 这类"连接还在但读不了库"的故障也能被发现。

### 10.4 指令一直停在 issued / progress（设备无回执）

**语义**：超过 `COMMAND_TIMEOUT_SECONDS`（默认 300s）无有效回执的指令，由后台巡检
`指令超时巡检` 置为 `failed`，`message=设备超时未回执（超过 N 秒）`，并同步刷新
OTA 任务 `failed` 计数。此后即使迟到 `success` 也不会复活。

**处置**：

1. `GET /api/devices/{deviceId}/commands` 确认 `state` 与 `updated_at`；
2. 查看设备是否在线、Topic ACL 是否允许订阅 `device/{deviceId}/cmd`；
3. 需要重试时**创建新指令/新 OTA 任务**，不要延长超时阈值来等待已失联设备；
4. 若单次 OTA 正常耗时就接近阈值，调大 `COMMAND_TIMEOUT_SECONDS` 与
   `COMMAND_SWEEP_INTERVAL_SECONDS`（compose 已透传，见 `infra/prod/.env.example`）。

### 10.5 Broker 停机与降级

Broker 停机时 `/api/health` 仍 200、`/ready` 的 `components.mqtt.connected=false`；
Web/移动端由 WebSocket 实时切换为 REST 轮询（见第 2 节指标）。Broker 恢复后网关自动重连，
设备重新上线后数据恢复。该场景已由既有 E2E `degrade_* / recover_*` 覆盖。

## 11. 移动端告警批量确认与设备搜索（v1.25.0，v1.26.0 改为加载更多）

**设备筛选（远程搜索）**

- 告警中心的设备筛选不再预拉全量设备，输入设备 ID 或名称后 300ms 防抖调用
  `GET /api/devices?keyword=<输入>&page=1&pageSize=20`。
- 下拉用可滚动区域展示**全部已加载结果**（v1.26.0 起不再只显示 4 条）；底部显示
  「共 N 台匹配，已加载 M 台」，并有「加载更多（还有 N-M 台）」按钮，可继续翻页直到全部加载。
- 结果不会被静默截断；若不想翻页，也可继续输入更精确的关键词缩小范围。
- 若网关 `keyword` 搜不到设备，先确认设备 `device_id` / `name` 是否包含输入片段（大小写不敏感）。

**批量确认（自动分批）**

- 网关单次 `POST /api/alerts/bulk-ack` 上限 100 条；移动端选中超过 100 条时自动按 100 条
  **串行**提交（第 N 批完成后再发第 N+1 批），不会并发轰炸网关。
- 各批结果合并展示：`acked` / `already_acked` / `not_found` 累加；单批失败不回滚已成功批次，
  摘要明确标出「第 X 批共 N 条提交失败」，失败批次保持选中便于直接重试。
- 若摘要出现「因登录失效，后续 N 条未提交」，说明 access/refresh 已失效，重新登录后重试即可；
  已成功批次不会重复确认。
- 排障：批次失败多为网络抖动或网关 5xx，按第 2 节 `/metrics` 与请求日志核对
  `POST /api/alerts/bulk-ack` 的状态码与耗时。

## 12. 批量指令与批量升级的标准操作（v1.28.0）

### 12.1 批量指令（config / calibrate / reboot）

**适用**：一次调整多台在线设备的运行参数、校准或重启。单次上限 **200 台**；
OTA 不在批量指令受理范围（`type=ota` 返回 422）。

1. 在 Web「设备管理」勾选设备（支持跨页保留，最多 200 台），点「批量指令」；
2. 选择类型与参数，确认弹窗会写清目标台数与「不可撤回」；
3. 提交后按 **成功 / 失败 / 跳过** 分类查看逐台原因：
   - 成功：指令已落库并投递；
   - 失败：设备可投递但下发失败（Broker 断开等）；
   - 跳过：设备不存在、设备离线；离线且 DB 无有效凭证元数据时会额外说明凭证问题。
4. 双击/网络重试不会重复下发：同一 `request_id` 在 10 分钟内返回首次结果
   （界面标注「未重复下发」）。刷新页面后重试会生成新 ID，视为一次新批次。

**判断是否需要回滚**：批量 `config` 下错参数时，立即用同一批设备下发**旧参数**（新批次），
不要逐台等待；`calibrate` 偏移错误时同样下发原偏移（通常为 0）。`reboot` 无需回滚，
只需观察设备是否在 300s 内重新上线。

审计：每次非重放批次一条 `command.bulk_create`，含 batch_id、requestId、类型、
成功/失败/跳过计数与抽样原因；可在审计页按 action 过滤。

### 12.2 批量 OTA

**入口**：设备列表勾选后点「批量 OTA」跳转 OTA 任务页，预选设备会带入；
也可直接在 OTA 页多选。单次上限 **200 台**，与批量注册对齐。

1. 选择目标版本与固件 URL；提交前预检会提示**离线**或**未签发/已吊销凭证**的设备台数
   与前 3 个 deviceId，不会静默失败；
2. 二次确认写清目标台数、失败风险与「任务下发后不能撤回」，建议在维护窗口执行；
3. 创建后查看任务卡片：`成功 / 失败 / 待回执 / 共` 汇总，展开可见逐台
   `state` / `progress` / `message`；
4. 单台失败**不会拖累其他设备**：v1.23.0 起任务按设备统计终态，`success`/`failed`
   达到 `total` 后不再接受状态变化；离线设备由超时巡检（默认 300s）置 `failed`。

**回滚判断**：

- 升级后功能异常且设备仍在线：创建新 OTA 任务回到上一个已知良好版本，不要复用原任务；
- 大面积失败（failed ≥ 30% 或核心分组全部失败）：先**停止继续扩大批次**，
  检查固件 URL 可达性、Broker 连接与凭证，再决定整体回滚；
- 设备失联：按第 10.4 节处理，不要延长超时阈值等待已失联设备；
- 回滚同样走「创建新任务」，历史任务保留用于审计与复盘。

---

## 13. 告警通知通道运维（v1.29.0，v1.31.0 增强）

> 配置入口：Web「系统设置」→「告警通知通道」。配置存库并热读取，
> 保存后下一条告警立即生效，**不需要重启网关**。
> 未配置或总开关关闭时，通知完全不发，告警仍照常入库与实时推送。

### 13.1 通道选型建议

| 场景 | 建议 | 理由 |
| --- | --- | --- |
| 已有 IM 值班群 / 值班机器人 | Webhook | 群消息到达快，适合 critical 即时提醒；可加 HMAC 防伪造 |
| 值班手机/邮箱、外部团队 | SMTP | 不依赖 IM；适合作为第二通道兜底 |
| 关键生产环境 | Webhook + SMTP 同时开启 | 单通道故障时另一条仍能触达；两条都失败会在投递记录中留痕 |
| 仅内部人员使用 App | 可暂不开启 | App 内角标与实时推送仍可用；但离开 App 的人员收不到提醒 |

**级别策略**：默认只发 `critical`。建议 warning 仅在有明确处理动作时开启，
否则会产生「通知疲劳」，导致 critical 被忽略。

### 13.2 值班配置（一次性）

1. 准备 Webhook 地址（群机器人 / 自建接收端）与可选的 HMAC 密钥（建议 ≥32 位随机串）；
   准备 SMTP 服务器、端口、账号、口令、发件人与值班收件人（多个用逗号分隔）。
2. Web「系统设置」填写通道并打开「启用通知」；保存。
3. 点「发送测试通知」→ 选择「全部通道」。结果会逐通道显示成功/失败原因，
   同时写入「最近投递记录」。
4. 到接收端确认：Webhook 应收到 `event=notification.test` 的 JSON；
   邮件主题应以 `[测试]` 开头。
5. 用一条真实 critical 告警复核：接收端收到 `X-Vikings-Event: alert` / 邮件主题
   `[CRITICAL]`。建议把这次验证结果记入值班交接。

**权限**：配置读写仅 admin；operator 可发测试通知（用于值班自检），viewer 不可见配置。
所有配置写入都会产生 `notify.config_update` 审计，只记录字段名，不记录取值。

### 13.3 静默窗口怎么用

- **两个不同的窗口，不要混淆**：
  - 告警入库抑制 `ALERT_SUPPRESS_SECONDS`（默认 300 秒）：同设备同指标在窗口内
    只**生成一条告警**；这是 App/告警中心看到的去重。
  - 通知静默 `mute_minutes`（默认 10 分钟）：告警照常入库并实时推送、在 App 内可见，
    只是**不重复发送通知**；投递记录写 `suppressed`。
- 值班配置建议：`mute_minutes` 取「同类告警的可接受提醒间隔」。设备抖动频繁时调大
  （例如 30 分钟），故障排查期可临时设 0 关闭通知去重。
- **抑制期内 App 仍可见**：静默只影响 Webhook/SMTP 发送，不修改 `device_alert`，
  也不影响 `device/system/alert` 的实时发布。移动端本轮不提供通知配置页，
  值班人员仍可在 App 告警中心看到每一条告警并确认处置。
- 设 0 表示关闭通知去重（每条符合规则的告警都会尝试发送）。

### 13.4 失败排查

只看「最近投递记录」，不要先翻日志：

| 记录 | 含义 | 处理 |
| --- | --- | --- |
| `sent` | 通道已接受 | 无需处理；到接收端确认是否被折叠/进垃圾箱 |
| `failed` + `HTTP 401/403` | Webhook 鉴权失败或密钥失效 | 检查接收端鉴权；更新 URL/密钥后发测试通知 |
| `failed` + `HTTP 404` | URL 路径错误 | 核对机器人/接收端地址 |
| `failed` + `HTTP 500/502/503` | 接收端故障 | 看接收端日志；网关已按指数退避重试到上限 |
| `failed` + `请求超时` | 出网慢或被防火墙拦截 | 检查 DNS、代理、安全组与目标端口 |
| `failed` + `SMTP <类型>` | 连接/认证/STARTTLS 失败 | 核对 host/port、账号口令、STARTTLS 开关、发件人域名策略 |
| `suppressed` | 静默窗口内被抑制 | 正常行为；需要立即提醒时临时调小 `mute_minutes` |

排查顺序：测试通知 → 投递记录 → 通道配置 → 出网/防火墙 → 接收端。
**通知失败不影响告警**：告警仍会入库并在 App/Web 显示；不要为了排查通知而回滚告警。

### 13.5 安全与审计

- Webhook URL 可能内含 token，**不要把密钥放 query string**；签名密钥走
  `X-Vikings-Signature`（HMAC-SHA256），接收端必须对原始 body 验签。
- SMTP 建议启用 STARTTLS 并使用专用发信账号/应用口令，收件人范围最小化。
- 网关日志不打印密钥、口令与完整 URL；投递 `error` 只保留状态码/异常类型。
  第三方 HTTP/SQL 驱动的 INFO/DEBUG 日志已统一压到 WARNING，避免 URL 与 SQL 参数泄露。
- 配置保存在数据库，数据库备份中同样包含这些敏感值：备份文件按密钥对待
  （见 5.1 的权限与存放要求），不要上传到公共网盘。
- 轮换：定期更换 Webhook 密钥与 SMTP 口令，保存后点测试通知复核；
  更换后检查审计 `notify.config_update` 与投递记录即可确认生效。

### 13.6 多规则优先级（v1.31.0）

一份通知配置下可配置多条规则（Web「系统设置」→「告警通知通道」→「接收规则」），
每条规则独立设置接收人（Webhook URL / 邮件收件人）、级别门槛与设备分组范围。

- **裁决方式：按 `priority` 升序取第一条匹配规则**，命中后只用该规则的接收人，
  不会叠加群发。同 `priority` 按创建顺序（ID）升序；界面表格即按此顺序展示。
  想「先匹配具体分组，再兜底全部设备」：具体规则给较小数字（如 10），兜底规则给较大数字
  （如 900），兜底规则的 `groups` 留空。
- 规则每次派发时读设备**当前**分组，改分组后下一条告警立即生效，不需要重启网关。
- **未命中任何规则时不会投递**（不会回退到全局默认），投递历史里记
  `suppressed / channel=rule / 未匹配任何通知规则`。理由：避免未分类设备被误发给
  默认值班人；并让「这条告警为什么没发」在历史页可直接回答。若确实需要兜底，
  请显式新增一条 `groups` 为空的兜底规则。
- v1.29.0 升级会从旧配置自动回填一条默认规则（优先级 100，沿用原 URL/收件人）；
  规则表被清空时运行期也回退到旧单配置行为。

### 13.7 重启期间的投递行为（v1.31.0）

- 待重试任务**已落库**（`notification_delivery.status=pending` + `next_attempt_at`），
  不是进程内队列；网关重启后调度器从表继续投递，无需人工重发。
- 重启窗口内：`pending` 行保留原计划时间；若已到期，重启后第一轮调度即会续投。
  停机越久，积压越多，但不会丢；`failed` 是终态，重启不会复活。
- **可能重复发送**：语义为 at-least-once。若进程在「HTTP 已发出、结果尚未写回」时被杀，
  重启后会再次尝试同一条。接收端应按 `X-Vikings-Delivery-Id`（Webhook）或确定性
  `Message-ID`（SMTP）做幂等去重。网关侧用「租约 + 状态机」避免并发重复，但不能
  消除上述崩溃窗口。
- 重试次数用尽（通道 `max_attempts`）后状态为 `failed`，`next_attempt_at` 置空，
  `error` 保留脱敏原因（状态码/超时/SMTP 异常类型），不会无限重试。
- 告警风暴时同时最多 3 条派发在跑，其余排队（仍先落库）；排队不占用数据库连接，
  MQTT 上报处理不会被慢接收端拖慢，通知延迟只体现在投递时间上。

### 13.8 值班交接：如何确认某条告警是否发出

入口：Web 左侧菜单「投递历史」（`/notifications`，operator/admin 可见；不进系统设置）。
支持按时间范围、通道（Webhook / 邮件 / 静默 / 规则）、状态（待投递 / 成功 / 失败 / 被抑制）、
设备 ID 组合筛选，服务端分页；每行展示尝试次数、累计耗时、最后错误、下次重试时间、
命中的规则与分组。

交接核对步骤：

1. 按时间范围 + 设备 ID 查该告警：有 `sent` 行即已送达对应接收人；看 `attempts` 判断是否补发过。
2. 有 `failed` 行：看 `error`（如 `HTTP 500`、`请求超时`、`SMTP SMTPAuthenticationError`），
   按 13.4 排查；`attempts` 应等于该通道的 `max_attempts`。
3. 有 `suppressed` 行：`channel=mute` 是静默窗口（正常）；`channel=rule` 是未命中规则，
   需要补规则或核对设备分组。
4. 完全没有任何行：说明告警未生成（先查告警中心），或通知总开关关闭。
5. 重启前后各看一次：重启后 `pending` 会变为 `sent`/`failed`，日志出现
   `通知重试成功` 或 `通知投递失败 ... attempt=n/max`。

## 14. 周报 / 月报导出与超大数据处理（v1.30.0）

入口：Web「历史查询」与「告警中心」的「导出 CSV」按钮。导出由网关流式生成，
不在浏览器拼表、也不翻页拉全量；筛选条件与页面列表完全一致。

### 14.1 周报 / 月报导出 SOP

1. **先定范围，再看总量**：历史页选设备（或告警页选设备/分组、级别、状态）与时间范围，
   先点「查询」确认条数与内容符合预期。导出使用当前筛选条件，不会额外放宽。
2. 点「导出 CSV」。按钮进入 loading 并显示已接收体积，期间不要重复点击。
   完成后文件名形如 `records_2026-11-01_2026-11-05.csv`、
   `alerts_2026-11-01_2026-11-05.csv`；未选时间范围时为 `<对象>_all_<当天>.csv`。
3. 用 Excel 双击打开即可（UTF-8 + BOM，中文表头不乱码）。**时间列是 UTC**，
   本地汇报需要按 Asia/Shanghai 加 8 小时；不要在文件里手工改表头，
   否则下次导出对账口径会不一致。
4. 对账前先核对行数：文件行数应等于页面「共 N 条」；若不等，检查是否在导出期间
   仍有新数据写入（时间范围最好截止到当天 00:00 或一个已经过去的整点）。
5. 导出动作会写审计 `export.records` / `export.alerts`（操作人、筛选、行数、耗时、文件名）。
   月报交付给客户前，建议把本次审计记录截图/抄录到交付记录里。

### 14.2 权限口径

| 角色 | 导出 | 说明 |
| --- | --- | --- |
| admin | ✅ | 全部数据 |
| operator | ✅ | 全部数据；日常周报/对账由运维执行 |
| viewer | ❌ 403 | 批量导出把大量数据带出系统，外泄面远大于单页查看；需要可追责账号 |

viewer 若确有只读报表需求，应由 admin 临时提升为 operator，用完降回；
不要为了省事把导出端点放开给 viewer。

### 14.3 超过 20 万行怎么办

单次导出上限 `EXPORT_MAX_ROWS`（默认 200,000）。超过时返回 **422** 并提示实际行数与上限；
这是有意为之——静默截断会让周报少行而无人发现。

正确处理顺序：

1. **按月 + 按分组拆批**：例如 90 天全量拆成 3 次月导出，或按 `group` 逐组导出后合并。
   每次导出都带明确 `start` / `end`，文件名自带范围，合并时容易核对。
2. **只导需要的列/对象**：对账通常只需告警或只需设备记录，不要一次导出两个对象再筛选。
3. **必要时临时调整上限**：仅限运维窗口，改环境变量 `EXPORT_MAX_ROWS` 后重启网关；
   调大后单次响应耗时与失败重试成本同步上升，完成后应恢复默认值。
4. 不要把上限调到无限：客户端断线会浪费一次全量查询，且大文件在浏览器侧仍需内存。

按当前实测（真实 PostgreSQL / 200,000 行 / 18.84 MB）：单次导出约 5.6 秒，
网关进程 RSS 峰值较基线仅增加约 8 MB；同数据量的分页 API 需要 100 次请求、
28.67 MB JSON 传输。因此**优先用导出端点**，不要用脚本循环翻页拼 CSV。

### 14.4 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 点击后立刻 422 | 筛选范围内行数超过上限 | 缩小时间范围或按设备/分组拆批（14.3） |
| 点击后 403 | 当前账号是 viewer | 换 operator/admin 账号（14.2） |
| 下载得到 JSON 错误体 | 浏览器/代理拦截或未登录 | 重新登录；确认未被反代改写 `Content-Type` |
| Excel 中文乱码 | 文件被二次转存为 ANSI | 重新导出原文件；原文件是 UTF-8 + BOM |
| 设备 ID 前导零丢失 | 用了「值粘贴」而非原文件 | 原文件该列是 `="001"` 文本公式，直接打开即可 |
| 行数与页面不一致 | 时间范围内仍有新数据写入 | 用已过去的整点作为 `end`，或先停写再导出 |
| 导出中断 | 网络断开/浏览器取消 | 审计仍会留一条记录；重新导出同一范围即可 |

### 14.5 大数据验证放在哪里

`scripts/verify_exports.py` 在真实 PostgreSQL 上造 20 万条记录与 20 万条告警，
启动真实 uvicorn 后验证文件大小/行数/耗时/RSS、CSV 兼容性、权限矩阵、超限 422 与审计内容。
它**不进常规 CI**：需要 Docker/本机 PostgreSQL、写入 40 万行且耗时与磁盘相关，
放进 7 个作业会让每个 PR 都付出几分钟并为环境抖动背锅。发布前或怀疑回归时手动执行，
输出贴入发布记录（v1.30.0 的实测数据见 `docs/releases/v1.30.0.md`）。

## 15. 告警 SLA 与值班交接（v1.33.0）

### 15.1 配置口径

系统设置 → 告警 SLA 可改四项（仅 `admin`；读取对所有登录角色开放）：

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| `critical_minutes` | 30 | critical 告警响应时限 |
| `warning_minutes` | 120 | warning 告警响应时限 |
| `max_escalations` | 2 | 单条告警最多升级次数；0 = 关闭升级 |
| `escalation_interval_minutes` | 30 | 相邻两次升级的间隔 |

时限在告警创建时写入快照（`sla_minutes` / `sla_deadline_at`），**改配置只影响新告警**。
存量告警保持原 SLA，避免换配置后历史口径漂移。历史确认但无 `acked_at` 的行归为
`unknown`，不计入达标（原因见 `docs/contracts.md` 3.3.3）。

### 15.2 超时升级为什么没发：按顺序排查

巡检默认每 30 秒一次（`SLA_SWEEP_INTERVAL_SECONDS`），日志关键字
「超时升级 alert=... attempt=N/M」。没发时依次看：

1. **告警是否真的超时**：`status=open` 且 `now() > sla_deadline_at`；已确认的不会升级。
2. **是否已达上限**：`escalation_count >= max_escalations` 后不再发送；达上限仍保持 `overdue`。
3. **是否还没到下一次间隔**：第 2 次升级要求 `deadline + interval` 已过，不是「每分钟一次」。
4. **通知总开关/静默窗口**：关闭或静默期内写 `suppressed`，在投递历史能看到原因。
5. **规则是否命中**：v1.31.0 多规则按 `priority` 首条命中；无规则或级别/分组不匹配记为
   `suppressed`。交接汇总有意忽略分组条件，但升级仍走完整规则。
6. **投递是否失败**：`notification_delivery.kind='escalation'` 的 `status` 为 `pending` /
   `failed` 时看 `last_error`；重试队列会在网关重启后继续（v1.31.0）。
7. **网关是否在跑巡检**：启动日志应有「SLA 巡检 30s」；没有说明调度器未启动。

### 15.3 值班交接流程

1. 交班前在 Web 告警页点「值班交接」；`from_user` 默认当前登录人。
2. 勾选「包含未指派告警」时，未指派与本班名下的未处理告警一起转给下一值班人；
   **他人名下的告警不会被动到**（避免越权接管）。
3. 二次确认后落库 `assignee`，写审计 `alert.handover`（交接人、接收人、条数、时间窗口），
   并向接收人发送**一条**汇总通知（`alert.handover`）。
4. 交接**不重置** SLA 截止时间与升级计数：已超时的仍在超时，已升级过的不会因换班重发。
   若确实需要重新计时，应关闭旧告警并新建（不建议用交接洗白 KPI）。
5. 接收人未收到汇总时：确认通知规则命中接收人所在分组/级别，查看投递历史
   `kind=handover`；交接更新本身已在审计中落库，不依赖通知成功。

### 15.4 权限

| 操作 | 所需角色 |
| --- | --- |
| 查看列表/SLA 状态 | 任意登录角色（viewer 只读） |
| 确认告警 | `operator` / `admin` |
| 值班交接 | `operator` / `admin` |
| 修改 SLA 配置 | 仅 `admin` |

## 16. 告警规则运维（v1.34.0）

### 16.1 规则从哪来、怎么裁决

- 升级时迁移 `0009_alert_rules` 会把 v1.33.0 的 10 个硬编码阈值写成 `is_default=true`
  的默认规则（范围 global、连续次数 1），阈值优先取 `rule_config` 的在线覆盖值，否则取环境变量。
  因此**升级后第一次上报的告警级别与阈值与升级前完全相同**。
- 规则配置入口：系统设置 → 告警规则（新增/编辑/启停/删除，均二次确认）。
  告警中心的旧「阈值配置」抽屉已移除，避免两处改同一件事。
- 同一指标多条规则命中时取最高级别（critical 胜 warning），同级按 `priority` 数字小者优先，
  每个指标只产一条告警。完整字段与口径见 `docs/contracts.md` 3.4。
- v1.35.0 起生效范围支持 `group` / `device` / `tag`，且都可填多个值。**同一条规则内多个值之间是
  「或」**（设备满足任一值即适用）；规则之间仍是「或」+ 最高级别裁决。分组、设备、标签不能混填在
  同一条规则里，否则返回 400。
- 规则全禁用时该指标不产生新告警，既有告警、统计与导出不受影响。

### 16.2 改阈值后为什么没生效：按顺序排查

1. **改的是哪条规则**：告警详情里的「触发规则」显示 `matched_rule.rule_name`。
   电压/湿度各有两条（warning 与 critical），只改 warning 时 2.9V 仍会被 critical 命中，
   看起来「没生效」。对照 `GET /api/rules` 的 `metric` / `op` / `level` / `scope_*` 四列。
2. **级别关系是否合法**：严重阈值必须比警告更严格；反向修改会返回 400 并说明原因，
   不会静默保存。先改 critical、再改 warning（或反过来）可避免中间态被拒。
3. **规则是否停用 / 范围是否匹配**：`enabled=false` 不参与判定；
   `scope_type=group` 匹配 `device.group_name`，`device` 匹配设备 ID，`tag` 匹配设备当前持有的
   任一标签（多值为「或」）。判定按**上报当刻**的分组/标签取值，改分组或改标签后下一条上报即生效。
4. **是否被更高级别或更高优先级规则覆盖**：同一指标只展示裁决后的一条；
   用 `/api/rules` 看所有同时命中项。
5. **抑制窗口**：同设备同指标在 `ALERT_SUPPRESS_SECONDS`（默认 300s）内只入库一条。
   验证新阈值时请换一台设备或等窗口结束，否则看到的是「抑制」而不是「新阈值」。
6. **是否多实例部署**：规则缓存在进程内，写入后 write-through 重载本进程；
   多实例网关各自持有缓存，需要每实例都执行一次规则写入（当前部署为单实例）。
7. **是否改的是旧接口**：`PUT /api/rules/thresholds` 仍可用，且会同步默认规则；
   但它只能改 10 个默认键，不能新增范围规则。新需求请用规则页。
8. **连续次数**：`consecutive_count>1` 时需连续 N 次越限才触发，单次上报不产生告警；
   中途一次正常上报会把计数清零。

### 16.3 回滚到默认规则

两种方式，均只影响默认规则，自定义规则保持不变：

1. **界面**：系统设置 → 告警规则 → 「恢复默认阈值」，二次确认后调用
   `POST /api/rules/restore-defaults`，写审计 `alert_rule.restore_defaults`。
2. **命令行**（与界面等价）：

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | jq -r .data.token)
curl -s -X POST http://127.0.0.1:8000/api/rules/restore-defaults \
  -H "Authorization: Bearer $TOKEN" | jq
```

恢复的是「环境变量/出厂值」，并会删除对应的 `rule_config` 覆盖行；
若线上仍希望长期保留某个阈值，请不要用恢复默认，而是把该值写回规则本身。
要彻底停用某指标告警，请**停用**规则而不是删除全部规则——删光后网关重启会按默认值重新补齐。

### 16.4 审计与验证

- 审计动作：`alert_rule.create/update/enable/disable/delete/restore_defaults`，
  `detail` 保留规则名、ID、变更字段名，并记录 `changes: {字段: {from, to}}` 前后值
  （v1.35.0 起；规则字段不含密钥，可以安全展示）。Web 系统设置 → 告警规则 → 「历史」
  直接读取这些审计记录，不另建平行表。
- 标签写操作审计 `device.tags_update`，记录设备数与标签集合；不记录设备凭证。
- v1.35.0 真实验证脚本：`scripts/verify_tags_v135.py`
  （真实 PostgreSQL + Alembic + mosquitto + MQTT 上报 + uvicorn + HTTP，覆盖迁移三场景、
  标签命中/非命中、多值「或」、批量部分失败、列表与导出行数、规则历史前后值）。

### 16.5 标签改名或删除后，规则会怎样

标签是设备上的**字符串值**，不是带独立 ID 的实体；系统没有「重命名标签」操作，
只有「给设备加标签 / 从设备移除标签」。因此：

1. **改名 = 先在所有设备上加新标签，再移除旧标签**。规则里写的是旧字符串时，
   移除完成前旧规则仍可能命中；移除完成后该规则不再匹配这些设备（范围不会自动迁移）。
   请按顺序操作：先加新标签 → 修改规则 `scope_values` → 再移除旧标签。
2. **删除标签 = 从设备移除该标签**（`device_tag` 行删除）。引用该标签的规则**不会自动删除或停用**，
   只是不再命中任何设备；规则仍显示在列表里，可在历史中看到其创建/修改记录。
3. **删除设备**（admin）会级联删除该设备的 `device_tag` 行（`delete_device` 先清理标签），
   相应标签的设备计数减少；规则本身不变。
4. **没有任何设备持有的标签**不会出现在 `GET /api/devices/tags` 的下拉里，但规则里仍可保留该值；
   重新有设备打上同名标签后规则立即恢复命中（匹配按字符串实时比较，无缓存）。
5. 若希望「某个标签的设备永久不再告警」，正确做法是**停用**对应规则，而不是删除全部规则；
   规则全删后网关重启会按默认阈值重新补齐默认规则。

### 16.6 为什么某设备没被规则命中：按顺序自查

1. **规则是否启用**：`enabled=false` 直接跳过。
2. **指标/方向是否对上**：规则 `metric` 与上报字段一致；`lt` 只看低于，`gt` 只看高于，
   等于阈值不触发。
3. **范围类型是否选错**：设备属于分组 A，但规则写的是 `scope_type=tag` 或 `device`；
   到设备详情确认它当前的分组与标签（不是历史值）。
4. **多值是否误以为「且」**：一条规则内多个标签是「或」，只要持有任一即命中；
   若希望「冷库且关键」，当前版本不支持，需要拆成更具体的分组/设备规则。
5. **是否被更高级别/更高优先级规则覆盖**：同指标只展示裁决后的一条；
   `GET /api/rules` 对照所有同时命中的规则，`matched_rule` 快照解释最终结果。
6. **连续次数**：`consecutive_count>1` 需要连续 N 次越限；中途一次正常值会清零。
7. **抑制窗口**：同设备同指标在 `ALERT_SUPPRESS_SECONDS`（默认 300s）内只入库一条，
   换设备或等窗口结束再看。
8. **规则缓存**：单实例网关 write-through；多实例部署需要每个实例都保存一次规则或重启刷新。

## 17. 设备影子同步、历史回滚与排障（v1.36.0 / v1.37.0）

### 17.1 单台与批量期望的标准操作

1. 在 Web「设备管理」打开设备详情，在「设备影子」核对期望、上报、差异键与同步徽章；
2. operator/admin 在 JSON merge patch 编辑器中提交，例如 `{"report_interval":30}`；
   `null` 表示删除期望键，也可以使用对应键的清除入口。viewer 只能查看；
3. 保存后以服务端返回的 `version` 与 `sync_status` 为准：同值 patch 显示无变化，
   不会空增版本；设备离线时显示「离线待同步」，不需要反复提交；
4. 批量操作在设备列表沿用分组/标签筛选与跨页勾选，点「批量期望」，二次确认后查看
   `matched / updated / unchanged / not_found` 和逐台结果。单次最多 200 台；
5. 设备下次上线/唤醒主动拉取并应用期望，再上报 `reported` 与已应用 `version`；
   差异清零后变为「已同步」。移动端详情提供同一份只读视图，不提供编辑器；
6. 需要核对或恢复旧配置时展开「变更历史」：Web 与移动端均可查看完整快照、动作、变化键、
   操作人和时间；只有 Web 的 operator/admin 可二次确认回滚，移动端没有写入口。

同步状态解释：

| 状态 | 处置 |
| --- | --- |
| `empty` | 当前没有期望；`reported` 中额外自由状态键不需要清理 |
| `synced` | 所有期望键已与上报一致，无需重复下发 |
| `pending` | 设备在线但仍有差异；先等设备本轮应用，再查设备同步日志 |
| `offline_pending` | 期望已保存，等设备下次上线；不要手工改库为 synced |

### 17.2 用模拟器验证上线同步

先取得 operator/admin access token（变量 `TOKEN`）与设备 MQTT 一次性口令，再执行：

```bash
GATEWAY_API_URL='http://127.0.0.1:8000' \
GATEWAY_API_TOKEN="$TOKEN" \
MQTT_DEVICE_PASSWORD='<设备 MQTT 口令>' \
python scripts/simulate_device.py --device-id dev-0001 --count 3 --interval 5
```

- 每轮发布 `online` 后，脚本 GET `/api/devices/dev-0001/shadow`；有未应用版本或差异时，
  模拟应用当前 `desired`，再 POST `/shadow/reported`，请求体为 `{reported, version}`；
  完整快照回滚移除的键会在本次 reported patch 中显式置 `null`，避免 merge 后残留旧值；
  已同步时不重复 POST。
- `--api-url` / `--api-token` 也可替代对应环境变量，两者必须同时提供；
  `--api-timeout` 默认 5 秒。未配置这些参数时完全保持原有 MQTT 上报/指令行为。
- 影子 REST 失败只打印「影子同步失败（继续本轮上报）」，不会阻断传感器与 OTA 联调；
  修复 token/网关后等下一轮自动重试。
- 这里的 token 是 operator/admin **用户 access token**，不是设备 MQTT 口令；默认 20 分钟过期。
  不要把 token 写入镜像、仓库或日志。脚本只用于联调，模拟上报不代表真实硬件已执行配置。
- 当前没有 shadow MQTT Topic 或网关实时推送；生产硬件若要完全设备级鉴权的 MQTT 影子链路，
  需要后续版本单独设计，不能把本模拟器当作该能力已交付。

### 17.3 影子一直未同步：按顺序排查

1. **看差异而不是只看版本**：GET 影子检查 `diff_keys` 与 `pending_desired`。
   `reported_version` 会防乱序后退并夹紧到当前版本，最终同步仍由字段值决定；
2. **看在线状态**：`offline_pending` 先按第 10.2 节处理连接、LWT 与凭证；
   设备不上线就不会拉取离线期望；
3. **看设备是否实现同步**：确认唤醒后确实 GET 影子、应用期望并 POST reported；
   仅发布传感器数据或 OTA 回执不会自动清除影子差异；
4. **看权限与过期**：POST reported 需要 operator/admin，viewer 为 403、token 过期为 401；
   模拟器日志会保留可读错误，重新登录获取 token 后下一轮重试；
5. **看 patch 是否合法**：最多 32 个扁平键，键长 1~32，值仅字符串/数字/布尔/null；
   嵌套对象与数组会被拒绝。`null` 在 desired/reported 中均表示清除对应键；
6. **看批量目标口径**：显式 ID、标签、分组同时出现时取交集；被筛选排除的现存设备不计入
   `matched`，也不会冒充 `not_found`。先在设备列表用同一条件确认目标，再提交；
7. **看审计**：单台 `device.shadow_desired_update`、批量 `device.shadow_desired_batch`、
   回滚 `device.shadow_rollback`。审计只含键名、版本、筛选与数量，不包含期望状态值；
   需要核值时读取当前影子或历史快照接口，不要改审计表。

### 17.4 用历史快照精确回滚（v1.37.0）

1. 在 Web 设备详情展开「变更历史」，按版本倒序核对目标快照的**完整 desired**，不要只看
   `changed_keys`；也可调用 `GET /api/devices/{id}/shadow/history?limit=50&offset=0` 分页核查；
2. operator/admin 对目标版本二次确认，或调用 `POST /api/devices/{id}/shadow/rollback`，
   请求体 `{"version":目标版本}`。回滚是完整替换而不是 merge：目标没有的当前键会删除，
   并生成一个新 `rollback` 版本；目标快照和旧历史保持不变；
3. 若当前期望已与目标快照一致，接口幂等返回当前影子，不空增版本、不重复写审计；
   目标版本不存在返回 404，不要直接改 `device_shadow.version` 或历史表伪造版本；
4. 回滚不会伪造设备已应用状态：`reported` 与 `reported_version` 保持原值。等设备下次唤醒
   拉取、实际应用并上报后再判断同步结果；被完整快照移除的旧配置键必须上报 `null` 清除；
5. 从 v1.36.0 升级时，`0012_shadow_history` 只为每台设备的当前期望补一条 `baseline`。
   升级前没有保存的更早版本无法恢复；downgrade 只删除历史表，不回退当前影子。



