# 交付就绪审计（v1.15.1，状态更新至 v1.19.0）

> 审计日期：2026-11-05
> 审计方式：只读代码与配置核对 + 本地真实命令验证；本轮不修改任何业务代码。
> 审计基线：`v1.15.0`（提交 `f77af2d`）
> 状态更新：P0-2 于 v1.16.0 完成、P0-3 于 v1.17.0 完成、P0-1 于 v1.18.0 完成；
> P1-2（数据生命周期 + 备份恢复）与 P1-3（迁移机制）于 v1.19.0 合并完成。
> P0 阻断项已全部关闭。

## 总体结论

**功能闭环、自动化测试与生产部署入口均已就绪。P0-2（默认凭据与登录防护）于 v1.16.0 完成，P0-3（Broker 鉴权与设备凭证）于 v1.17.0 完成，P0-1（生产部署入口：Dockerfile / 反代 / HTTPS）于 v1.18.0 完成并已真实起栈验收；P1-2（数据生命周期与备份恢复）与 P1-3（Alembic 迁移机制）于 v1.19.0 合并完成。** 三个 P0 阻断项全部关闭，可以交付；数据层现在具备可重复、可回滚的变更与备份能力。

已确认做得好、不需要返工的部分：

- 所有写操作都有审计：`grep audit_service.log(` 覆盖登录、设备、告警、指令、OTA、阈值、账号，动作常量集中在 `gateway/app/services/audit_service.py`；
- 已有 `/api/health` 存活探针并暴露 Broker 连接状态（`gateway/app/api/routes/stats.py:22-35`）；
- 自动化基线真实可跑：gateway `pytest` 87 条、Flutter 67 条、Web 7 条、真实 Broker E2E 25/25；
- OTA 代码链路完整（任务创建 → 指令下发 → 模拟器进度回执），只是缺 E2E 覆盖（`gateway/app/api/routes/ota.py:14-47`、`scripts/simulate_device.py:111-116`）。

---

## P0（阻断交付，必须做）

### P0-1 生产部署入口缺失：无 Dockerfile、无反代/HTTPS，一键脚本仅支持 macOS ✅ 已完成（v1.18.0）

- **现状证据**
  - `infra/docker-compose.yml:1-4` 明确写着「本文件只编排基础设施；生产部署请为 gateway / web 补 Dockerfile」；实际仅 `mosquitto` 与 `postgres` 两个服务（同文件 `services:` 下）。
  - 全仓 `find . -maxdepth 4 \( -iname 'Dockerfile*' -o -iname 'nginx*' -o -iname 'Caddyfile*' \)` 无输出。
  - `scripts/start_all.sh:12-15` 使用 `/opt/homebrew/...` 绝对路径，`README.md:122-126` 前置依赖是 `brew install ...`；Linux/云主机无法直接跑。
  - `README.md:140-146` 入口全部是 `http://127.0.0.1:*`，无 TLS/域名/反代说明。
- **缺口**：拿到仓库的人无法在 Linux/云主机上一条命令起完整服务；生产只能裸 HTTP，移动端与 Web 的凭据在网络上明文传输。
- **建议做法**
  1. 新增 `gateway/Dockerfile`（Python 3.12 + uvicorn）、`web/Dockerfile`（Node 构建 + nginx 静态托管）；
  2. 扩展 `infra/docker-compose.yml` 增加 `gateway` / `web` / `reverse-proxy` 服务与 healthcheck，复用现有 `mosquitto` / `postgres`；
  3. 提供 `infra/Caddyfile` 或 nginx 配置（443 + 自动证书 + WebSocket 升级 9001/API 代理）；
  4. 新增 `.env.production.example` 与 `docker compose up -d` 启动说明；保留 `start_all.sh` 作为 macOS 开发路径。
- **预估工作量**：2~3 人日（含一次云主机实测）。
- **依赖关系**：依赖 P0-2 的密钥注入方式与 P0-3 的 Broker 凭证配置；可先起骨架，最后联调。
- **完成证据（v1.18.0）**
  - **镜像**：`gateway/Dockerfile`（`python:3.12-slim` 多阶段、uid/gid 10001 非 root、
    `HEALTHCHECK` 要求 `mqttConnected == true`）；`web/Dockerfile`（`node:20-alpine` 构建 →
    `nginxinc/nginx-unprivileged:1.27-alpine` 静态托管，监听 8080）。
    本机 `docker build` 均 exit 0：`iot-gateway:verify` 345MB、`iot-web:verify` 78.9MB。
  - **生产编排**：`infra/docker-compose.prod.yml`（`name: iot-prod`）含 postgres / mosquitto /
    gateway / web / proxy；`depends_on.condition: service_healthy`、`restart: unless-stopped`、
    命名卷持久化、`backend` 网络 `internal: true`；只发布 80/443 与 1883，
    gateway:8000 / web:8080 / broker:9001 / postgres:5432 均不出网。
  - **反代与 HTTPS**：`infra/prod/nginx/` 提供 80→443 跳转、ACME webroot、TLS 1.2/1.3、
    HSTS/nosniff/DENY/Referrer-Policy/Permissions-Policy/CSP；`/api` 透传 Authorization 与
    `X-Forwarded-*`，`= /mqtt` 升级 WebSocket；登录 `limit_req` 以 snippet 形式可选启用。
  - **一键启动**：`scripts/prod_up.sh` 只依赖 docker/compose/openssl/python3，首次自动生成
    `infra/prod/.env`（随机 JWT/PG/管理员口令、`DEPLOY_UID/GID`）、Mosquitto 服务/客户端凭证、
    自签证书，再 `up -d --build` 并轮询 5 服务健康（最多 180s）；拒绝 root 部署。
  - **密钥治理**：`infra/prod/.env.example` 列全量必填项并标注生成方式；`.env`、`secrets/`、
    `cert/`、ACME 目录与 Broker 数据全部进 `.gitignore`；`.dockerignore` 排除密钥与构建产物。
  - **真实起栈验收（macOS + colima，HTTP_PORT=8080 HTTPS_PORT=8443）**
    - 5 服务全部 `healthy`：postgres、mosquitto、gateway、web、proxy；
    - `https://localhost:8443` 管理员登录 `token_len=144`；`mqtt-credentials` web/mobile
      均 `code:0`（口令长度 43）；`/api/devices` total=4（dev-0001..dev-0004）；
      `/api/records/latest` 返回真实记录（id=5，23.42°C，`2026-11-04T18:43:26.714Z`）；
    - HTTP 80 返回 301，HTTPS 响应含全部安全头；
    - `simulate_device.py` 带凭证走 1883 上报成功，WSS `GET /mqtt` 101 + CONNACK rc=0，
      收到 `device/dev-0001/sensor/report`，与 REST latest 一致；
    - 匿名 `tcp:1883` 与 `websockets:8443/mqtt` 均 CONNACK rc=135（回归 P0-3）；
    - `APP_ENV=production` + 默认密钥运行镜像退出码 3，列出 JWT/ADMIN/DB/MQTT 缺失或弱值
      （回归 P0-2）；
    - `docker compose down`（保留卷）后 `up -d`：5 服务重新 healthy，设备仍为 4 台，
      latest 记录 id/温度/时间戳完全一致（持久化成立）。
  - **CI**：新增 `images` 作业，只构建 gateway/web 镜像，不起栈；既有四作业保持不变。
  - **文档**：`docs/deployment.md`（从零部署、端口网络、证书、升级、备份衔接、故障排查、
    人工验收清单、已知边界）、`docs/operations.md`（启停/日志/凭证轮换/备份恢复/回滚/
    证书续期）、README 顶部生产部署入口、`docs/contracts.md` 1.2 反代与外部端点。
  - **回归**：gateway `pytest` 87 passed；`flutter analyze` 无问题；`flutter test` 67 passed；
    `web npm run test` 7 passed；`web npm run build` 成功；真实 E2E 25/25 PASS。

### P0-2 默认密钥与演示账号可直接登录，且无登录限流 ✅ 已完成（v1.16.0）

- **现状证据**
  - `gateway/app/config.py:71-84`（审计时 33-36）：`jwt_secret = "change-me-in-production"`、`admin_username = "admin"`、`admin_password = "admin123"`（注释写「禁止硬编码」，但默认值仍是硬编码口令）。
  - `gateway/app/services/user_service.py:35-75`（审计时 25-68）：表为空时无条件创建 `operator/operator123`、`viewer/viewer123`，并与默认 admin 一起落库；`README.md:148-150` 公开写明这些账号。
  - `gateway/app/schemas.py:100-102` 登录入参只要求 `min_length=1`；`gateway/app/api/routes/auth.py:16-52`（审计时 16-33）无失败计数、无锁定、无限流。
  - `infra/docker-compose.yml:27-30` PostgreSQL 口令硬编码 `iot_pass`。
  - `gateway/app/config.py:72` token 固定 720 分钟，`gateway/app/security.py` 无刷新/吊销机制。
- **缺口**：公网暴露即等同默认管理员权限；暴力破解无成本；口令可任意短；数据库口令随仓库分发。
- **建议做法**
  1. 启动时若 `jwt_secret` 仍为默认值或 `admin_password` 仍为 `admin123` 且 `APP_ENV=production`，直接 fail-fast 拒绝启动；
  2. 生产关闭演示账号种子（`SEED_DEMO_USERS`，生产默认关闭），`admin_password` 从 secret 注入；
  3. 登录限流（网关内进程内计数按 IP+账号；多进程部署需反代层或 Redis 做全局限制）；
  4. 新建/重置口令最小长度与弱口令校验（已实现）；PostgreSQL 口令的 compose 注入随 P0-1 落地；
  5. token 短 TTL / 刷新 / 吊销不在本轮范围，见 P2-4。
- **预估工作量**：1~2 人日。
- **依赖关系**：与 P0-1 的环境变量体系一起落地；限流可独立先做。
- **完成证据（v1.16.0）**
  - `gateway/app/config.py` 新增 `APP_ENV`、`validate_for_production()`（密钥长度 + 已知弱值 + 管理员口令 + PostgreSQL 口令）与 `security_warnings()`；
  - `gateway/app/main.py:35-42` 在 lifespan 最先执行生产校验，失败即拒绝启动；日志中的数据库地址改为脱敏；
  - `gateway/app/services/user_service.py` 的 `ensure_bootstrap_users` 由 `demo_users_enabled` 控制，生产默认不种演示账号；
  - `gateway/app/services/login_guard.py` + `gateway/app/api/routes/auth.py` 实现 IP+账号限流，超限 429 + `Retry-After`，成功登录清零；
  - `gateway/app/security.py` 的 `validate_password_strength()` 接入新建/重置账号；
  - 新增 `gateway/tests/test_auth_protection.py` 11 条用例，gateway `pytest` 78 passed；
  - README 4.2.1 必改项、`docs/contracts.md` 429/口令/种子语义、`docs/releases/v1.16.0.md` 已同步。

### P0-3 Broker 匿名接入，设备无凭证/无 ACL ✅ 已完成（v1.17.0）

- **现状证据**
  - `infra/mosquitto/mosquitto.conf:7,12`：1883 与 9001 均 `allow_anonymous true`，注释自认「生产环境请关闭」。
  - `gateway/app/models.py:32-49` 的 `Device` 无凭证/密钥字段；`gateway/app/api/routes/devices.py` 的注册/导入只建元数据，不签发设备身份。
  - `gateway/app/config.py:27-28` 网关自身支持 `mqtt_username/password`，但没有按设备维度的账号体系。
- **缺口**：任何能访问 1883/9001 的人都能伪造上报、状态、告警、指令回执，污染数据并绕过云端权限（Web 端 JWT 只能保护 REST，保护不了 MQTT）。
- **建议做法**
  1. Mosquitto 启用 `password_file` + `acl_file`，设备只能发布自己的 `device/{id}/...`、订阅自己的 `cmd`；
  2. 网关新增设备凭证签发/轮换（注册或首次激活时下发，密码只存哈希/加密值）；
  3. Web/移动端连接使用独立只读账号或短期 token（9001 侧按角色限 Topic）；
  4. 开发环境保留匿名 profile，生产 profile 强制鉴权；contracts 与部署文档同步。
- **预估工作量**：2~3 人日（含凭证生命周期与迁移）。
- **依赖关系**：依赖 P0-1 的部署 profile 划分；与设备注册流程（当前仅元数据）一起改。
- **完成证据（v1.17.0）**
  - `infra/mosquitto/mosquitto.conf`：`per_listener_settings true`，1883 与 9001 均
    `allow_anonymous false` + `password_file passwd` + 各自 `acl_file`；重载方式写入注释。
  - 新增 `infra/mosquitto/acl.device`（网关跨设备读写 + 设备 `device/%u/#` 只读自己、
    仅可写自己的 report/status/cmd/ack）与 `infra/mosquitto/acl.client`
    （`web-console` / `mobile-console` 只有 read，无任何 write）。
  - 新增 `gateway/app/mqtt/credentials.py`（PBKDF2-SHA512 哈希、0600 原子写）
    与 `scripts/mqtt_credentials.py`（init / issue / rotate / revoke / show）。
  - `Device` 新增 `mqtt_username`、`mqtt_credential_version`、`mqtt_credential_fingerprint`、
    `mqtt_credential_issued_at`、`mqtt_credential_rotated_at`，只存元数据与指纹，不存明文；
    `infra/postgres/init.sql` 与 `gateway/app/database.py` 同步补列。
  - 新增 `POST /api/devices/{deviceId}/mqtt-credential`（operator+admin，签发/轮换，
    口令只返回一次，写 `passwd` 后 SIGHUP/自定义命令重载）；
    新增 `GET /api/auth/mqtt-credentials?client=web|mobile`（登录后下发只读凭证，未配置 503）。
  - Web `web/src/services/mqtt.ts` 与移动端 `realtime_transport.dart` / `api_client.dart`
    改为先取凭证再连接，取不到不匿名降级；设备模拟页与 `simulate_device.py` 支持口令，缺口令拒绝连接。
  - `start_all.sh` 首次自动初始化凭证并注入网关环境变量；`infra/docker-compose.yml`
    挂载整个配置目录并设置 `working_dir`；`passwd` / 状态文件已加入 `.gitignore`。
  - 新增 `gateway/tests/test_mqtt_credentials.py`；生产安全 fixture 补 MQTT 凭证校验。
  - E2E 增至 25 项并全部 PASS：匿名 1883/9001 CONNACK rc=135/135；跨设备订阅/发布收不到；
    Web/移动端可订阅但发布被拒；轮换后旧凭证 rc=135、新凭证 rc=0。
  - README「4.2.2 Broker 鉴权与凭证管理」、`docs/contracts.md` 1.1 鉴权矩阵与凭证接口、
    `docs/releases/v1.17.0.md` 已同步。

> P0 共 3 项：P0-2 已在 v1.16.0 完成、P0-3 已在 v1.17.0 完成、P0-1 已在 v1.18.0 完成；
> **P0 阻断清单已清空**。不把「告警备注」「指标美化」「依赖扫描」等体验/质量项混入阻断清单。

---

## P1（可交付，但上线后会被运维/客户抱怨）

### P1-1 可观测性只有存活探针，缺请求日志、指标与自身健康告警

- **现状证据**
  - `gateway/app/main.py:26-29` 仅 `logging.basicConfig` 文本日志，无请求 ID / 访问日志 / 状态码与耗时。
  - `gateway/app/api/routes/stats.py:22-35` 的 `/api/health` 只返回 `mqttConnected` 等字段，不查数据库，无法区分「进程活着但 DB 不可用」。
  - 全仓 `grep -RilE 'prometheus|opentelemetry|sentry' gateway/app web/src` 无业务侧命中；无 `/metrics`。
- **缺口**：线上 5xx、慢查询、Broker 掉线、DB 故障没有统一观测入口；也没有「网关自身健康」的告警规则。
- **建议做法**：JSON 结构化日志 + 请求 ID 中间件；新增 `/api/health/ready`（DB `SELECT 1` + Broker 状态）；暴露 Prometheus 指标（请求数/延迟/5xx、MQTT 消息数、告警未处理数）；为 `mqttConnected=false`、5xx 比例、健康探针失败配置告警。
- **预估工作量**：1~2 人日。
- **依赖关系**：无强依赖，可与 P0-1 并行；指标暴露依赖部署侧抓取配置。

### P1-2 数据生命周期只清理 records，告警/审计无限增长，且无备份恢复方案 ✅ 已完成（v1.19.0）

- **现状证据**
  - `gateway/app/background_tasks.py:45-70` 只删除 `SensorRecord`，保留期来自 `gateway/app/config.py:47`（90 天）。
  - `device_alert` 与 `audit_log` 无任何清理逻辑（同上文件仅 import `Device, SensorRecord`）。
  - `gateway/app/models.py:77-80` 告警索引只有 `created_at` 与 `(device_id, metric, created_at)`，没有 `status` 索引；而告警中心默认查 `status=open`。
  - `grep -nE '备份|pg_dump|restore|恢复策略' README.md docs/*.md` 无结果。
- **缺口**：告警表长期膨胀、open 查询随数据量变慢；误删/磁盘故障无恢复路径。
- **建议做法**：按表配置保留期（如 acked 告警 180 天、审计 365 天），分批删除；新增 `(status, created_at)` 索引；补充 `pg_dump` 定时备份 + 恢复演练 runbook（含 SQLite 演示路径）。
- **预估工作量**：1~2 人日。
- **依赖关系**：与 P1-3 的迁移机制一起做，避免直接改表无版本记录。
- **完成证据（v1.19.0）**
  - `gateway/app/background_tasks.py`：`purge_expired_data` 统一执行三类清理（分块提交）：
    records 按 `ts` 保留 90 天；**只删 acked 告警（默认 180 天），open 永不自动删除**；
    审计日志默认 365 天。`gateway/app/config.py` 新增 `alert_retention_days`、`audit_retention_days`。
  - 手动触发：`python -m app.maintenance purge [--dry-run]`（容器内
    `$COMPOSE exec -T gateway python -m app.maintenance purge`），与定时任务同一实现。
  - 迁移 `0002_lifecycle_indexes` 新增 `idx_alert_status_created`、`idx_alert_level_created`、
    `idx_alert_status_device`、`idx_audit_operator_created`，并提供 `downgrade`。
  - 真实 PostgreSQL EXPLAIN 证据（20 万告警 / 5 万审计）：
    `status='open'` 列表命中 `idx_alert_status_created`；角标聚合 `status+device_id`
    由 14.7ms 降至 2.5ms；`operator=op-7` 审计查询 0.606ms 降至 0.037ms。
  - `scripts/pg_backup.sh`（gzip/时间命名/保留 N 份/失败非零退出）与
    `scripts/pg_restore.sh`（输入库名确认、`--yes`、恢复后打印 `alembic_version`）。
  - 隔离容器恢复演练：20 万告警/5 万审计/5 万记录备份 2.4MB → 清空告警、删半审计、
    改名与新增设备 → 恢复后计数、设备名、`dev-999` 全部回到备份时点，
    `schema_version=0002_lifecycle_indexes`（详见 `docs/operations.md` 5.3）。
  - 测试：`tests/test_data_lifecycle.py`（SQLite + 真实 PostgreSQL），
    `test_system_service` 状态接口暴露保留期配置；CI 新增 PostgreSQL 作业。

### P1-3 无正式数据库迁移机制，`create_all` + 手写补列不可持续 ✅ 已完成（v1.19.0）

- **现状证据**
  - `gateway/app/database.py:53-77`：启动时 `Base.metadata.create_all`，仅手写补 `device.group_name` 一列。
  - `infra/postgres/init.sql` 只在容器首次初始化执行（`infra/docker-compose.yml:36`），后续结构变更无版本化。
  - 全仓无 `alembic` / `migrations` 目录。
- **缺口**：升级已有环境时新列/新索引/类型变更容易漏，回滚无依据；SQLite 与 PostgreSQL 两套结构易漂移。
- **建议做法**：引入 Alembic，以当前结构为 baseline，后续每次变更生成 revision；CI 增加「空库升级到 head」与「旧库升级」测试；保留 `create_all` 仅用于测试。
- **预估工作量**：1~2 人日。
- **依赖关系**：P1-2 的表结构变更应排在迁移机制之后。
- **完成证据（v1.19.0）**
  - `gateway/alembic/`：`0001_baseline`（v1.18.0 及之前的完整结构，检测到既有 `device`
    表时跳过建表并标记已应用）+ `0002_lifecycle_indexes`（查询索引，含 `downgrade`）。
  - `gateway/app/database.py`：启动路径移除 `create_all` 与手写 `ALTER TABLE`；
    `prepare_database()` 开发/测试自动 upgrade，生产只校验 `alembic_version == head`，
    不一致拒绝启动。`create_all` 仅保留在测试辅助中。
  - 生产编排新增 `migrate` 一次性服务（`alembic upgrade head`），gateway
    `depends_on service_completed_successfully`；迁移失败不启动网关。
    `infra/postgres/init.sql` 不再挂载到生产，只服务本地开发。
  - 测试 `tests/test_migrations.py`：空库 upgrade、既有库自动标记（存量设备保留）、
    显式 `stamp 0001_baseline`、`downgrade -1` 往返、事件循环内启动迁移。
  - CI：gateway 作业新增空库迁移 + 降级往返 + `alembic check`；
    新增 `postgres` 作业（postgres:16 service）跑迁移与生命周期测试。
  - 真实生产卷验收：v1.18 的 `iot-prod_postgres-data`（含 4 台设备）升级后日志显示
    `[baseline] 检测到既有 schema，跳过建表`，`alembic_version=0002_lifecycle_indexes`，
    设备数仍为 4，HTTPS 登录与查询正常；`down`/`up` 后数据仍在。

### P1-4 Web 后台未复用批量确认，告警操作能力落后于移动端

- **现状证据**
  - `web/src/api/alerts.ts:18-22` 只有单条 `ackAlert`，全文件无 `bulk-ack`。
  - `web/src/views/AlertsView.vue:8` 只导入 `ackAlert`；页面无多选与批量操作。
  - 网关 `POST /api/alerts/bulk-ack` 已在 v1.15.0 提供（`gateway/app/api/routes/alerts.py`）。
- **缺口**：同一产品两端操作能力不一致；Web 值班场景仍要逐条点确认。
- **建议做法**：Web 告警页增加多选 + 批量确认，复用同一端点；错误按 `acked/already_acked/not_found` 分类提示。
- **预估工作量**：0.5~1 人日（含路由守卫/组件测试）。
- **依赖关系**：无；可直接复用既有接口。

### P1-5 移动端无可发布产物：release 仍用 debug 签名，CI 无构建/制品/依赖扫描

- **现状证据**
  - `device_app/android/app/build.gradle.kts:32-37`：release `signingConfig = signingConfigs.getByName("debug")`，注释仍是 TODO。
  - `.github/workflows/ci.yml:50-66` 的 `device_app` 作业只有 `pub get / analyze / test`，无 `flutter build`、无 `upload-artifact`。
  - 全仓无 `.github/dependabot.yml`；`web/package.json` 无 `audit` 脚本，CI 无依赖安全扫描。
  - v1.15.0 CI 运行日志出现 Node.js 20 弃用提示（actions/checkout@v4、setup-node@v4 被强制跑在 Node 24）。
- **缺口**：无法产出可安装的正式包；每次发版靠本地手工；供应链风险无基线。
- **建议做法**：`key.properties` + CI secrets 配置 release 签名；CI 增加 `flutter build apk --release`（或 appbundle）并上传 artifact；启用 Dependabot（npm/pub/pip/actions）；升级 actions 到 v5/v6 消除 Node 20 提示；可选加入 `npm audit` / `pip-audit` 门禁。
- **预估工作量**：1~2 人日。
- **依赖关系**：签名密钥需在仓库 secrets 配置，需仓库管理员配合。

### P1-6 缺部署/运维手册，README 只覆盖 macOS 开发路径 ✅ 已完成（v1.18.0）

- **现状证据**
  - `README.md:120-176` 快速开始全部围绕 `brew`、本机地址与开发脚本；`docs/` 仅有 `architecture.md` / `contracts.md` / `releases/`。
  - 部署、反代、证书续期、备份恢复、升级回滚均无独立文档；全仓 `grep` 备份相关无结果。
  - `infra/docker-compose.yml` 注释要求阅读者自行补生产配置，但没有对应文档。
- **缺口**：交接/上线依赖原作者口述；新人按 README 只能跑开发环境。
- **建议做法**：新增 `docs/deployment.md`（Docker/反代/HTTPS/环境变量/端口/凭据）与 `docs/operations.md`（监控、备份恢复、升级回滚、证书续期、故障排查）；README 快速开始保留并链接。
- **预估工作量**：1 人日。
- **依赖关系**：P0-1、P1-2 落地后按实际命令编写，避免文档与实现再次漂移。
- **完成证据（v1.18.0）**
  - 新增 `docs/deployment.md`：前置条件、两步启动、端口与网络隔离、自签与 ACME/正式证书替换、
    环境变量表、健康检查与日志、升级回滚、备份衔接、故障排查、人工验收清单、已知边界；
  - 新增 `docs/operations.md`：启停与状态、日志查看、凭证轮换（设备/吊销/客户端/新增）、
    备份与恢复、回滚、证书续期、数据保留与安全例行检查；
  - `README.md` 顶部新增「生产部署（v1.18.0）」区块，指向以上两份文档。
  - P1-2 的定时备份与恢复演练仍待补，文档中已给出当前可用的 `pg_dump`/卷备份命令。

### P1-7 测试底线存在明确盲区：OTA、PostgreSQL、故障注入与真机验证

- **现状证据**
  - 本地真实执行：gateway `pytest -q` 87 条、`flutter test` 67 条、`npm run test` 7 条、E2E 25/25 PASS。
  - `scripts/e2e/e2e_acceptance.py` 覆盖上报/状态/告警/指令/分组/批量确认，但 `grep -n 'ota|OTA'` 无命中——OTA 未进 E2E。
  - 网关测试使用临时 SQLite（`gateway/tests/test_http_regression.py` 的 `sqlite+aiosqlite`），CI 无 PostgreSQL 作业。
  - 无「数据库不可用/磁盘满/网络超时」故障注入；无真机/真网络自动化（Widget 测试注入 fake）。
- **缺口**：OTA 端到端、PostgreSQL 方言差异、基础设施故障路径只能手工验；测试全绿不代表生产同构可用。
- **建议做法**：E2E 增加 OTA 任务（创建 → 模拟器进度 → 成功/版本更新）；CI 增加 Postgres service 跑一遍 pytest 与迁移；为 DB 断连、Broker 断连补网关降级测试；发布前保留一份真机手工清单（弱网、切后台、权限拒绝、推送）。
- **预估工作量**：2~3 人日。
- **依赖关系**：OTA E2E 依赖 P1-5 的模拟器/发布环境无关，可直接做；Postgres CI 依赖 P1-3 迁移骨架。

---

## P2（体验与质量，不阻断交付）

### P2-1 告警级别口径三端不一致

- **现状证据**：规则引擎仅产出 `warning` / `critical`（`gateway/app/rules.py` 与 `docs/contracts.md` 的阈值表）；移动端告警中心暴露 info/notice/warning/critical 四个筛选（`device_app/lib/pages/alert_center_page.dart` 的 `_levelOptions`）；Web 告警页筛选仍为 warning/critical（`web/src/views/AlertsView.vue`）。
- **缺口**：用户会问「info/notice 为什么永远为空」；两端筛选能力也不对齐。
- **做法**：明确产品口径——要么规则引擎补充 info/notice 产出，要么移动端隐藏这两级（保留接口兼容）；文档写清楚。
- **预估**：0.5 人日。**依赖**：无。

### P2-2 告警处置备注缺失

- **现状证据**：`gateway/app/models.py:73-94` 的 `DeviceAlert` 无备注字段；`POST /api/alerts/{id}/ack` 只改状态；`web/src/api/alerts.ts` 与移动端批量确认均无备注入参。
- **缺口**：确认后无法记录「谁、为什么、怎么处理」，审计只有动作没有处置内容。
- **做法**：新增 `ack_note` / `acked_by` / `acked_at` 字段与可选入参，单条与批量都支持；列表展示处置人/备注。
- **预估**：1~1.5 人日。**依赖**：P1-3 迁移机制。

### P2-3 移动端规模化上限写死在页面

- **现状证据**：设备筛选一次拉 200 台（`alert_center_page.dart` 的 `fetchDevices(pageSize: 200)`）；批量确认上限 100 条（`gateway/app/schemas.py` 的 `AlertBulkAckRequest`）。
- **缺口**：超过 200 台后部分设备无法在下拉中选择；超过 100 条要手工分批。
- **做法**：设备筛选改远程搜索/下拉分页；批量确认在客户端自动分批并汇总 `acked/not_found`。
- **预估**：1 人日。**依赖**：无。

### P2-4 认证生命周期与 API 安全头不完整

- **现状证据**：`gateway/app/config.py:34` 固定 720 分钟；无 refresh/ logout/吊销；`gateway/app/main.py` 的 CORS 白名单按 `cors_origins` 配置，但无 CSP/HSTS/安全响应头。
- **缺口**：token 泄露后 12 小时内无法失效；浏览器侧缺纵深防御。
- **做法**：短 TTL + refresh token + 服务端 token 版本号（吊销）；反代加 HSTS/CSP/X-Frame-Options；CORS 生产按域名收紧。
- **预估**：1~2 人日。**依赖**：P0-1 反代、P0-2 token 策略。

### P2-5 CI/运维体验细节

- **现状证据**：`.github/workflows/ci.yml:17-18,37-40` actions 仍为 v4/v5，v1.15.0 运行出现 Node 20 弃用提示；无缓存策略说明（已有 npm cache）；无构建耗时/失败趋势看板。
- **缺口**：提示噪音、长期维护成本。
- **做法**：升级 actions、按需加 `concurrency` 取消旧运行、把 E2E 日志 artifact 化（失败时上传）。
- **预估**：0.5 人日。**依赖**：无。

---

## 建议执行顺序

**第一批（P0，串行启动、部分可并行）**

1. ~~P0-2 默认凭据与登录限流~~ ✅ v1.16.0。
2. ~~P0-3 Broker 凭证/ACL~~ ✅ v1.17.0：双监听关闭匿名、设备凭证与轮换、客户端只读凭证、ACL 隔离与 E2E。
3. ~~P0-1 容器化 + 反代/HTTPS~~ ✅ v1.18.0：镜像、生产编排、HTTPS 反代与真实起栈验收。
   - 三者可在同一迭代并行设计，但**合并验收必须串行**（先密钥/凭证，再部署）。

**第二批（P1，可并行分组）**

- 可并行：P1-1 可观测性、P1-4 Web 批量确认、P1-5 移动端发布工程（互不冲突）。
- 需串行：~~P1-3 迁移机制 → P1-2 数据保留/索引~~ ✅ v1.19.0 合并完成（数据层可重复、可回滚）；
  ~~P1-6 运维文档~~ ✅ v1.18.0 完成。
- P1-7 测试补强：PostgreSQL CI ~~等 P1-3~~ ✅ 已在 v1.19.0 加入。

**第三批（P2，按客户反馈排序）**

- 优先 P2-1 级别口径与 P2-2 处置备注（直接影响演示解释成本），其余随迭代补齐。

**建议版本切分**

- `v1.16.0`：P0-2 默认凭据与登录防护 ✅ 已完成；
- `v1.17.0`：P0-3 Broker 鉴权与设备凭证 ✅ 已完成；
- `v1.18.0`：P0-1 + P1-6（生产部署入口与运维/部署手册）✅ 已完成；
- `v1.19.0`：P1-2 + P1-3（Alembic 迁移、数据生命周期、备份恢复）✅ 已完成；
- 下一批：P1-1 可观测性、P1-4 Web 批量确认、P1-5 移动端发布工程、P1-7 剩余测试补强；
- P2 项穿插或单独小版本。
