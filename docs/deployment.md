# 生产部署指南（v1.19.0）

> 目标：**一台干净的机器拿到仓库，执行 2 步即可起一套可对外服务的完整栈**
> （PostgreSQL + Mosquitto + Python 网关 + Vue 管理后台 + HTTPS 反代）。
>
> 日常启停、日志、凭证轮换、备份与回滚见 [operations.md](operations.md)；
> Broker 鉴权矩阵见 [contracts.md](contracts.md#11-broker-鉴权与主体topic-矩阵v1170)。

---

## 1. 前置条件

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 操作系统 | Linux（推荐）或 macOS | 生产建议 Debian/Ubuntu LTS |
| 容器运行时 | Docker Engine 24+ + Compose v2 | `docker compose version` 必须可用 |
| 资源 | ≥ 2 vCPU / 2 GB 内存 / 10 GB 磁盘 | 单网关 + PostgreSQL + Broker |
| 端口 | `80`、`443`、`1883` 可对外 | 80 用于跳转与 ACME；1883 给设备 |
| 域名（可选） | A 记录指向服务器 | 没有域名也可先用 IP + 自签证书验证 |
| 工具 | `openssl`、`python3` | 首次生成密钥、密码文件与自签证书 |
| 部署用户 | **非 root** | 网关容器按该用户 uid 运行；脚本会拒绝 uid=0 |

macOS 本地验证说明：

- Docker Desktop 可直接映射 80/443；使用 colima 时，非 root 用户无法绑定 <1024 端口，
  请用 `HTTP_PORT=8080 HTTPS_PORT=8443` 覆盖（本文验收示例即如此）。
- 生产模板的默认端口仍是 80/443。

---

## 2. 两步启动

```bash
git clone <仓库地址> Vikings && cd Vikings

# 生产：直接执行（首次会自动生成全部密钥、MQTT 凭证与自签证书）
./scripts/prod_up.sh

# 本机 colima 验证低端口受限时：
HTTP_PORT=8080 HTTPS_PORT=8443 SEED_DEVICE_IDS=dev-0001 ./scripts/prod_up.sh
```

`prod_up.sh` 的实际动作：

1. 检查 `docker`、`docker compose`、`openssl`、`python3`；
2. 首次从 `infra/prod/.env.example` 生成 `infra/prod/.env`（权限 600），自动填入：
   `JWT_SECRET`、`POSTGRES_PASSWORD`、`ADMIN_PASSWORD`、`DEPLOY_UID`、`DEPLOY_GID`；
3. 生成 Mosquitto 服务/客户端凭证到 `infra/prod/secrets/mosquitto.passwd`，
   并将口令回填到 `.env`（`MQTT_PASSWORD` / `MQTT_WEB_PASSWORD` / `MQTT_MOBILE_PASSWORD`）；
   `SEED_DEVICE_IDS=dev-0001,dev-0002` 可顺便签发设备凭证（默认不签）；
4. 生成自签 HTTPS 证书到 `infra/prod/nginx/certs/`（已存在则跳过）；
5. `docker compose -f infra/docker-compose.prod.yml --env-file infra/prod/.env up -d --build`；
   compose 会先启动一次性 `migrate` 服务执行 `alembic upgrade head`，成功后 gateway 才启动；
6. 轮询 5 个常驻服务的健康检查，最多 180 秒，全部通过才返回成功。

> 所有密钥、密码、证书都落在 gitignored 路径，仓库只有模板与生成脚本。
> 需要定制管理员口令时，在首次执行前设置 `ADMIN_PASSWORD='<强口令>'`。

验证入口：

```bash
docker compose -f infra/docker-compose.prod.yml --env-file infra/prod/.env ps
curl -k https://localhost/api/health          # colima 验证用 8443
curl -k https://localhost/healthz             # 反代自身存活
```

---

## 3. 端口与网络隔离

| 端口 | 暴露范围 | 用途 |
| --- | --- | --- |
| `80` | 公网 | HTTP → HTTPS 跳转；`/.well-known/acme-challenge/` |
| `443` | 公网 | Web 静态资源、`/api/*` REST、`/mqtt` MQTT over WebSocket |
| `1883` | 公网（设备） | MQTT over TCP，强制账号口令 + ACL |
| `8000` | 仅容器网络 | 网关（不发布到宿主机） |
| `8080` | 仅容器网络 | Web 静态托管（不发布到宿主机） |
| `9001` | 仅容器网络 | Broker WebSocket（只能经反代 `/mqtt` 访问） |
| `5432` | 仅容器网络 | PostgreSQL（`backend` 网络标记 `internal: true`，不出网） |

网络划分：

- `edge`：`proxy`、`web`、`gateway`、`mosquitto`；
- `backend`（internal）：`gateway`、`postgres`；
- 数据库没有任何宿主机端口，也不在 `edge` 网络，公网不可达。

浏览器与移动端的实时链路统一走 `wss://<host>/mqtt`，由反代转给 Broker 的 9001；
公网不需要开放 9001。

---

## 4. HTTPS 证书

### 4.1 本地/内网验证（默认）

`prod_up.sh` 会用 `scripts/gen_self_signed_cert.sh` 生成：

```
infra/prod/nginx/certs/fullchain.pem   # 自签证书，CN=域名，SAN 含 localhost/127.0.0.1
infra/prod/nginx/certs/privkey.pem     # 私钥，0600
```

- 浏览器访问会出现「不受信任」提示，需要手动信任；
- `curl` 验证用 `-k`，或 `--cacert infra/prod/nginx/certs/fullchain.pem`；
- HSTS 头已下发，但自签证书下浏览器不会把它持久化，这是**验证边界**，不代表生产缺失。

### 4.2 生产替换为正式证书

证书文件名固定为 `fullchain.pem` / `privkey.pem`，直接替换即可：

```bash
# 方式 A：已有正式证书/商业证书
cp <your-fullchain>.pem infra/prod/nginx/certs/fullchain.pem
cp <your-privkey>.pem   infra/prod/nginx/certs/privkey.pem
chmod 644 infra/prod/nginx/certs/fullchain.pem
chmod 600 infra/prod/nginx/certs/privkey.pem
docker compose -f infra/docker-compose.prod.yml --env-file infra/prod/.env restart proxy
```

```bash
# 方式 B：Let's Encrypt（HTTP-01，宿主机 certbot）
# 先把域名 A 记录指向服务器，并确保 80 端口可达
sudo certbot certonly --webroot \
  -w "$PWD/infra/prod/nginx/acme" \
  -d iot.example.com --email ops@example.com --agree-tos
sudo cp /etc/letsencrypt/live/iot.example.com/fullchain.pem infra/prod/nginx/certs/
sudo cp /etc/letsencrypt/live/iot.example.com/privkey.pem   infra/prod/nginx/certs/
docker compose -f infra/docker-compose.prod.yml --env-file infra/prod/.env restart proxy
```

- ACME 挑战目录已挂载到反代的 `/var/www/acme`；
- 续期后重复复制并 `restart proxy`，或把上述命令放进宿主机 cron；
- 也可以把证书交给外部负载均衡/云厂商终止 TLS，此时反代仅监听内网，需自行调整 `proxy` 的 ports；
- 私钥不得入库；`infra/prod/nginx/certs/` 已在 `.gitignore` 中。

---

## 5. 环境变量

模板：[infra/prod/.env.example](../infra/prod/.env.example)。`prod_up.sh` 首次生成 `infra/prod/.env`（600），
可人工修改；重复执行不会覆盖已有值。

| 变量 | 必填/生成 | 说明 |
| --- | --- | --- |
| `PUBLIC_ORIGIN` | 建议改 | 浏览器公开源（CORS 白名单、自签证书 CN），如 `https://iot.example.com` |
| `HTTP_PORT` / `HTTPS_PORT` / `MQTT_PORT` | 默认 80/443/1883 | 仅本机验证低端口受限时覆盖 |
| `POSTGRES_USER` / `POSTGRES_DB` | 默认 iot | 数据库账号/库名 |
| `POSTGRES_PASSWORD` | **自动生成，必改** | `openssl rand -hex 24`；compose 用它拼 `DATABASE_URL` |
| `JWT_SECRET` | **自动生成，必改** | `openssl rand -hex 48`，≥32 字符，禁止仓库默认值 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 口令自动生成，必改 | 生产启动会拒绝弱口令；首次生成可传 `ADMIN_PASSWORD` |
| `PASSWORD_MIN_LENGTH` | 默认 8 | 新建/重置账号的最小长度 |
| `SEED_DEMO_USERS` | 固定 false | 生产不种 operator/viewer |
| `LOGIN_*` | 默认 | 网关内 IP+账号失败锁定 |
| `MQTT_USERNAME` | 固定 `iot-gateway` | 与 ACL 的服务身份一致，生产校验会强制 |
| `MQTT_PASSWORD` | 脚本生成 | 网关接 Broker 的口令 |
| `MQTT_WEB_USERNAME` / `MQTT_WEB_PASSWORD` | 脚本生成 | Web 只读凭证 |
| `MQTT_MOBILE_USERNAME` / `MQTT_MOBILE_PASSWORD` | 脚本生成 | 移动端只读凭证 |
| `MQTT_PASSWORD_FILE` | 固定容器路径 | `/run/secrets/mosquitto.passwd` |
| `MQTT_PASSWORD_FILE_MODE` | 固定 644 | 网关卡与 Broker 容器以不同 uid 共享文件 |
| `DEPLOY_UID` / `DEPLOY_GID` | **自动生成，必改** | `id -u` / `id -g`；网关以该非 root 用户运行 |
| `LLM_*` / `ALERT_WEBHOOK_URL` | 可选 | 不配则 AI 降级模板、告警不外推 |
| `DATA_RETENTION_DAYS` | 默认 90 | 传感器时序保留天数（按上报时间 `ts`） |
| `ALERT_RETENTION_DAYS` | 默认 180 | 仅清理已确认（acked）告警；open 永不自动删除 |
| `AUDIT_RETENTION_DAYS` | 默认 365 | 审计日志保留天数 |
| `DATA_PURGE_INTERVAL_SECONDS` | 默认 3600 | 清理任务执行周期 |
| `TZ` | 可选 | 默认 `Asia/Shanghai` |

安全约束：

- 生产启动（`APP_ENV=production`）会 fail-fast：默认/弱 `JWT_SECRET`、弱管理员口令、
  SQLite、弱/缺失数据库口令、缺失或错误的 MQTT 服务身份都会拒绝启动；
- `.env`、`infra/prod/secrets/`、`infra/prod/nginx/certs/` 全部 gitignored；
- 不要把真实口令写进 README、compose 或镜像；镜像构建只接收非敏感前端构建参数。

---

## 6. 健康检查、重启策略与日志

| 服务 | 健康检查 | 依赖 |
| --- | --- | --- |
| `postgres` | `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB` | — |
| `migrate` | 一次性任务：`alembic upgrade head`，退出码 0 视为完成 | postgres healthy |
| `mosquitto` | 用 `iot-gateway` 身份 `mosquitto_pub` 一条状态报文 | — |
| `gateway` | `/api/health` 且 `data.mqttConnected == true` | migrate 完成、postgres、mosquitto healthy |
| `web` | `wget http://127.0.0.1:8080/` | — |
| `proxy` | `wget http://127.0.0.1/healthz` | gateway、web healthy |

- 常驻服务 `restart: unless-stopped`，异常退出自动重启；`migrate` 为 `restart: "no"` 的一次性任务；
- 迁移失败时 gateway 不会启动，避免「代码新、库旧」的隐性故障；
- 日志轮转：单服务 `max-size=10m`、`max-file=3`；
- 网关健康检查**要求 Broker 已连接**，凭证错误时容器保持 unhealthy 而不是假装就绪。

---

## 7. 升级与回滚

```bash
# 1. 先备份（结构变更前必须）
./scripts/pg_backup.sh --keep 30

# 2. 拉取新代码并重建
git fetch --tags && git checkout <新版本 tag>
./scripts/prod_up.sh

# 3. 验证迁移与健康
docker compose -f infra/docker-compose.prod.yml --env-file infra/prod/.env logs migrate | tail -20
docker compose -f infra/docker-compose.prod.yml --env-file infra/prod/.env exec -T postgres \
  sh -c 'psql -tAc "select version_num from alembic_version" -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
curl -k https://<host>/api/health
docker compose -f infra/docker-compose.prod.yml --env-file infra/prod/.env ps
```

- 首次部署与升级都由 `migrate` 服务自动执行迁移；既有库由 baseline 自动识别，
  **不会重建或清空**（详见 [operations.md](operations.md#43-已有库v1180-及更早)）；
- 空库首次初始化后没有演示设备，设备在首次上报时自动登记，或由管理员通过
  `POST /api/devices` 创建。

回滚：

```bash
git checkout <上一个稳定 tag>
PROD_SKIP_BUILD=0 ./scripts/prod_up.sh
```

结构也要回退时，先按 [operations.md](operations.md#6-回滚) 恢复升级前的 `pg_dump`，再启动旧版本。
不要执行 `alembic downgrade base`（会删除全部业务表）。

---

## 8. 备份与恢复衔接

```bash
# 备份 PostgreSQL（推荐每日 cron；脚本负责压缩、命名、保留 N 份、失败非零退出）
./scripts/pg_backup.sh --keep 30

# 备份 Broker 持久化（retained/离线队列）
docker run --rm -v iot-prod_mosquitto-data:/data -v "$PWD/backups":/backup alpine \
  tar czf "/backup/mosquitto-$(date +%F).tar.gz" -C /data .
```

恢复流程、保留策略、cron 与异地存放建议、真实恢复演练见
[operations.md](operations.md#5-备份与恢复)。

---

## 9. 常见故障排查

| 现象 | 排查 | 处理 |
| --- | --- | --- |
| `gateway` 一直 unhealthy | `logs gateway` 看是否 fail-fast 或连接 Broker 失败 | 核对 `.env` 的 JWT/ADMIN/DB/MQTT；确认 `infra/prod/secrets/mosquitto.passwd` 存在且 644 |
| `migrate` 退出非 0，gateway 未启动 | `logs migrate` | 若是半升级状态（缺表）先人工核对；一般修正数据库连接后重跑 `$COMPOSE run --rm migrate` |
| `gateway` 报「数据库迁移版本不一致」 | `SELECT version_num FROM alembic_version` | 先执行 `$COMPOSE run --rm migrate alembic upgrade head` |
| `mosquitto` unhealthy | `logs mosquitto`；健康检查用的 `iot-gateway` 口令是否与 passwd 一致 | 重新执行 `prod_up.sh`（幂等回填）；或轮换 `service:iot-gateway` 后同步 `.env` |
| 反代 502 | `logs proxy gateway` | 等 gateway health 通过；检查 compose 网络是否同时连 `edge` |
| 浏览器 WebSocket 连不上 | 控制台看 `wss://host/mqtt`；`logs proxy` | 路径必须是 `/mqtt`；确认 443 可达、证书受信任 |
| `curl` 证书错误 | — | 自签阶段用 `-k`/`--cacert`；生产替换正式证书 |
| 登录 401 | 账号是否 `ADMIN_USERNAME`；口令是否在 `.env` | 从 `.env` 读取；不要使用仓库示例口令 |
| 登录 429 | 触发 IP+账号锁定 | 等待 `LOGIN_LOCKOUT_SECONDS`；或调整阈值并重启 gateway |
| 端口占用 | `lsof -iTCP:80`/`443`/`1883` | 停掉旧栈或改 `HTTP_PORT`/`HTTPS_PORT`/`MQTT_PORT` |
| `permission denied` 写 passwd | 网关 uid 与 secrets 目录属主不一致 | 用部署用户重新执行 `prod_up.sh`，不要用 root 运行 |
| 构建拉镜像失败 | `docker info`、代理/镜像源 | 修正 Docker 网络或 registry mirror 后重跑 |

---

## 10. 人工验收清单（自签证书边界内）

以下命令与本机真实验收一致；`HOST` 按实际端口替换（生产 443，colima 验证 8443）。

```bash
# 1) 两个镜像构建
docker build -f gateway/Dockerfile -t iot-gateway:verify .
docker build -f web/Dockerfile -t iot-web:verify .

# 2) 全栈健康
docker compose -f infra/docker-compose.prod.yml --env-file infra/prod/.env ps

# 3) 管理员登录（从 .env 读取口令，不要打印到终端历史）
ADMIN_USER=$(grep -E '^ADMIN_USERNAME=' infra/prod/.env | cut -d= -f2)
ADMIN_PASS=$(grep -E '^ADMIN_PASSWORD=' infra/prod/.env | cut -d= -f2)
TOKEN=$(curl -sk -X POST https://localhost:8443/api/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["token"])')

# 4) 取 Web / 移动端 MQTT 只读凭证
curl -sk -H "Authorization: Bearer $TOKEN" \
  'https://localhost:8443/api/auth/mqtt-credentials?client=web'
curl -sk -H "Authorization: Bearer $TOKEN" \
  'https://localhost:8443/api/auth/mqtt-credentials?client=mobile'

# 5) 设备列表与最新值
curl -sk -H "Authorization: Bearer $TOKEN" \
  'https://localhost:8443/api/devices?page=1&page_size=20'
curl -sk -H "Authorization: Bearer $TOKEN" \
  'https://localhost:8443/api/records/latest'

# 6) 设备带凭证走 1883 上报
DEVICE_PASS=$(python3 -c 'import json; d=json.load(open(".run/mqtt/prod-credentials.json")); print(d["devices"]["dev-0001"]["password"])')
MQTT_DEVICE_PASSWORD="$DEVICE_PASS" python3 scripts/simulate_device.py \
  --device-id dev-0001 --host localhost --port 1883 --count 1

# 7) 匿名连接应被拒绝（rc=5 / paho 135）
python3 - <<'PY'
import paho.mqtt.client as mqtt
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="anon-check")
rc, _ = c.connect("localhost", 1883, 5)
print("anonymous 1883 rc =", rc)
PY

# 8) 生产默认密钥 fail-fast（期望非 0 退出）
docker run --rm -e APP_ENV=production iot-gateway:verify
```

第 8 步使用镜像内默认配置，必然因默认 `JWT_SECRET`/`admin123`/SQLite 拒绝启动；
这是 P0-2 的回归验证，不是部署失败。

---

## 11. 已知边界

1. **自签证书**：本机验收只能证明 HTTPS 链路、安全头与代理语义正确，
   不能证明公网浏览器信任链；正式上线必须替换证书（见 4.2）。
2. **凭证轮换后的 Broker 重载**：网关容器无法向另一个容器的 Broker 发 SIGHUP，
   API 轮换会写 passwd 但 `reloaded=false`；需按 operations.md 重启/重载 Broker 才生效。
   宿主机 CLI 轮换可直接随后 `docker compose ... restart mosquitto`。
3. **单实例**：gateway 目前是单进程；多副本需要把登录限流与 MQTT 消费协调改为外部组件
   （P1-1/P2-4）。
4. **空库无演示数据**：生产首次初始化由迁移建表，不再执行开发用的 `init.sql` 种子；
   设备在首次上报时自动登记（`ensure_device`）。
5. **本机 Docker**：若目标环境不允许容器运行时，按第 10 节命令清单在有 Docker 的机器复验；
   不要以「配置看起来正确」替代真实起栈。
