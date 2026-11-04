# 运维手册（v1.19.0）

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

## 2. 日志与排障

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
网关健康检查要求 `mqttConnected=true`，因此 Broker/凭证问题会直接表现为网关 unhealthy。
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

```bash
python3 scripts/mqtt_credentials.py rotate \
  --subject device:dev-0001 \
  --password-file infra/prod/secrets/mosquitto.passwd \
  --state-file .run/mqtt/prod-credentials.json

$COMPOSE restart mosquitto
```

- 新口令只在命令输出中出现一次，立即写入设备安全存储；
- 旧口令在 Broker 重载后失效；
- 设备端断线会重连，旧凭证将收到 CONNACK rc=5/135。

### 3.2 吊销设备凭证

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

---

## 4. 数据库迁移（Alembic）

v1.19.0 起，表结构只由 `gateway/alembic/versions/` 中的迁移文件管理：

- 启动路径不再执行 `Base.metadata.create_all` 或手写 `ALTER TABLE` 补列；
- 生产编排的 `migrate` 一次性服务在 gateway 启动前执行 `alembic upgrade head`，
  失败时 gateway 不会启动；
- 版本记录在 `alembic_version` 表，`0001_baseline` 对应 v1.18.0 及之前的结构，
  `0002_lifecycle_indexes` 为 v1.19.0 的查询索引。

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
# 期望：0002_lifecycle_indexes（以仓库 head 为准）

# 4) 健康检查
curl -k https://<host>/api/health
```

若只改代码不改结构，`migrate` 也会执行一次 `upgrade head`，已是 head 时为空操作。

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
0 2 * * * cd /opt/vikings && ./scripts/pg_backup.sh --keep 30 >> logs/backup.log 2>&1
```

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
