# 运维手册（v1.18.0）

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

- 全部服务 `restart: unless-stopped`；服务器重启后会随 Docker 自启。
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

# 最近一次启动失败的容器日志
$COMPOSE ps -a
docker logs --tail=200 <container>
```

排障顺序：`postgres` → `mosquitto` → `gateway` → `web` → `proxy`。
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

## 4. 备份与恢复

### 4.1 备份

```bash
mkdir -p backups
# PostgreSQL
$COMPOSE exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  | gzip > "backups/postgres-$(date +%F-%H%M).sql.gz"

# Broker 持久化数据（retained 状态、离线队列）
docker run --rm -v iot-prod_mosquitto-data:/data -v "$PWD/backups":/backup alpine \
  tar czf "/backup/mosquitto-$(date +%F-%H%M).tar.gz" -C /data .

# 密钥与配置（离线加密保存，禁止入库）
tar czf "backups/secrets-$(date +%F).tar.gz" \
  infra/prod/.env infra/prod/secrets infra/prod/nginx/certs
```

建议每日数据库备份 + 变更前手动备份；保留周期按合规要求，至少保留 7 天。

### 4.2 恢复

```bash
# 1) 停止写入
$COMPOSE stop gateway

# 2) 恢复数据库（会覆盖现有库，务必确认）
gunzip -c backups/postgres-<timestamp>.sql.gz | \
  $COMPOSE exec -T postgres sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'

# 3) 需要时恢复 Broker 数据
$COMPOSE stop mosquitto
docker run --rm -v iot-prod_mosquitto-data:/data -v "$PWD/backups":/backup alpine \
  sh -c 'tar xzf /backup/mosquitto-<timestamp>.tar.gz -C /data'

# 4) 启动并验证
$COMPOSE start mosquitto gateway
$COMPOSE ps
curl -k https://<host>/api/health
```

恢复后建议抽查设备列表、最新上报与告警记录是否一致。

---

## 5. 回滚

```bash
git fetch --tags
git checkout <上一个稳定 tag>       # 例如 v1.17.0
./scripts/prod_up.sh                # 重新构建并滚动替换

# 若数据库结构被新版本改动且无法兼容：
#   先恢复升级前的 pg_dump，再启动旧版本。
```

回滚前务必确认已备份；不要用 `docker compose down -v` 清空数据。

---

## 6. 证书续期

```bash
# Let's Encrypt 续期（宿主机）
sudo certbot renew --webroot -w "$PWD/infra/prod/nginx/acme"
sudo cp /etc/letsencrypt/live/<domain>/fullchain.pem infra/prod/nginx/certs/
sudo cp /etc/letsencrypt/live/<domain>/privkey.pem   infra/prod/nginx/certs/
$COMPOSE restart proxy
```

建议把续期放进 cron，并在续期后触发 `restart proxy`；或用外部负载均衡统一终止 TLS。

---

## 7. 数据保留与维护

- 后端默认 `DATA_RETENTION_DAYS=90`，清理任务每小时执行一次（`data_purge_interval_seconds`）；
- 告警与审计日志目前不会自动清理（P1-2），长期运行需自行归档或加清理任务；
- 定期检查磁盘：`docker system df`、`df -h`；
- 日志已按容器轮转（10 MB × 3）；如需集中日志，接入 Loki/ELK 等（P1-1）。

---

## 8. 安全例行检查

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
