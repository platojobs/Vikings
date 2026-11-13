# 接口契约（全端唯一事实来源）

> 本文件是 Flutter 设备端、Python 网关、Vue3 后台三端开发的唯一契约。
> 任何一端改动都必须先改本文件，禁止私自扩展字段。

---

## 1. MQTT Topic 规范

| 用途 | Topic | 方向 | QoS | Retain |
| --- | --- | --- | --- | --- |
| 传感器数据上报 | `device/{deviceId}/sensor/report` | 设备 → 网关/后台 | 1 | 否 |
| 设备状态上下线 | `device/{deviceId}/status` | 设备 → 网关/后台 | 1 | 是 |
| 云端下发指令 | `device/{deviceId}/cmd` | 后台/网关 → 设备 | 1 | 否 |
| 设备指令回执（含 OTA 进度） | `device/{deviceId}/cmd/ack` | 设备 → 网关/后台 | 1 | 否 |
| 系统 AI 告警 | `device/system/alert` | 网关 → 后台 | 1 | 否 |
| 设备分组变更 | `device/system/group` | 网关 → 后台 | 1 | 否 |

设计说明：

- `{deviceId}` 为设备唯一标识，全链路统一使用同一字符串（如 `dev-0001`）。
- 状态 Topic 使用 **Retain + LWT（Last Will and Testament）**：设备上线发布 `online`（retained），
  Broker 在设备异常掉线时自动代为发布遗嘱 `offline`，网关无需依赖心跳超时才判定离线。
- 指令回执 `cmd/ack` 是原需求「OTA 进度反馈」的标准化承载方式，避免为 OTA 单独开一类 Topic。

### 1.1 Broker 鉴权与主体—Topic 矩阵（v1.17.0）

Broker 的 1883（MQTT over TCP）与 9001（MQTT over WebSocket）**均关闭匿名**，
使用 `per_listener_settings true` 分监听加载各自的 `password_file` 与 `acl_file`；
账号口令由 `scripts/mqtt_credentials.py` 生成，`infra/mosquitto/passwd` 不入库。

| 主体 | 监听 | 身份来源 | 可读 Topic | 可写 Topic |
| --- | --- | --- | --- | --- |
| 网关 `iot-gateway` | 1883 | 网关环境变量 `MQTT_USERNAME` / `MQTT_PASSWORD` | `device/+/sensor/report`、`device/+/status`、`device/+/cmd/ack` | `device/+/cmd`、`device/system/alert`、`device/system/group`、`gateway/status` |
| 设备 `<deviceId>` | 1883 | 设备凭证，用户名恒等于 `deviceId` | `device/<deviceId>/#`（自己的整棵子树） | `device/<deviceId>/sensor/report`、`device/<deviceId>/status`、`device/<deviceId>/cmd/ack` |
| Web `web-console` | 9001 | 登录后 `GET /api/auth/mqtt-credentials?client=web` 下发 | `device/+/sensor/report`、`device/+/status`、`device/+/cmd/ack`、`device/system/alert`、`device/system/group` | 无（ACL 不含任何 write） |
| 移动端 `mobile-console` | 9001 | 登录后 `GET /api/auth/mqtt-credentials?client=mobile` 下发 | 同上 | 无（ACL 不含任何 write） |
| 匿名 | 1883 / 9001 | — | 无 | 无（CONNACK 拒绝，rc=5 / paho 映射 135） |

关键约束：

- 设备**不能发布** `device/<deviceId>/cmd`（指令只能由网关下发），也不能写 `device/system/*`；
- 设备可读自己的整棵子树，用于自检与回读；`%u` 只替换为当前连接用户名，读不到其他设备；
- Web / 移动端是独立只读主体，即使拿到口令也无法通过 9001 发布设备上报 Topic；
- Mosquitto 对 ACL 拒绝的订阅可能仍返回 SUBACK granted，**隔离以投递结果为准**（收不到消息）。

### 1.2 生产反代与外部端点（v1.18.0）

生产部署只暴露 `80/443/1883`；Broker 的 9001、网关 8000、Web 8080 与 PostgreSQL 5432
都只在 Docker 内网可达。外部访问语义如下：

| 外部端点 | 内部目标 | 说明 |
| --- | --- | --- |
| `https://<host>/api/*` | `gateway:8000` 同路径 | REST 契约不变；`Authorization` 透传，追加 `X-Forwarded-For/Proto` |
| `https://<host>/mqtt` | `mosquitto:9001` | MQTT over WebSocket；Mosquitto 固定要求路径 `/mqtt` |
| `https://<host>/` | `web:8080` | 管理后台静态资源与 SPA 回退 |
| `http://<host>/` | — | 301 跳转 HTTPS（`/.well-known/acme-challenge/` 除外） |
| `<host>:1883` | `mosquitto:1883` | 设备 MQTT over TCP，账号口令 + ACL 不变 |

客户端地址推导（v1.18.0 起）：

- **Web**：生产构建写入 `VITE_API_BASE=""`、`VITE_MQTT_WS="/mqtt"`，
  运行时按页面协议拼成同源 `https://<host>/api` 与 `wss://<host>/mqtt`；
- **移动端**：网关填 `https://<host>` 时自动推导 `wss://<host>/mqtt`；
  开发 HTTP 仍推导 `ws://<host>:9001`；也可在实时地址字段手动覆盖；
- 客户端凭证仍由 `GET /api/auth/mqtt-credentials?client=web|mobile` 下发，
  反代只透传请求，不参与鉴权，也不改变任何 Topic 语义。

反代附加的安全头（不影响 JSON 契约）：

- `Strict-Transport-Security`（HSTS）、`X-Content-Type-Options: nosniff`、
  `X-Frame-Options: DENY`、`Referrer-Policy: strict-origin-when-cross-origin`、
  `Permissions-Policy`、CSP（`default-src 'self'`，允许同源 `wss` 与内联样式）。
- 登录路径 `/api/auth/login` 支持可选 `limit_req`（默认关闭，网关自身已有 IP+账号锁定）；
  `limit_req_status=429`，与网关限流的 429 语义一致。

自签证书验证时 `curl` 需 `-k` 或 `--cacert`；正式部署必须替换为受信任证书。

---

## 2. MQTT 消息体（JSON，UTF-8）

### 2.1 传感器上报 `device/{deviceId}/sensor/report`

```json
{
  "deviceId": "dev-0001",
  "temperature": 24.6,
  "humidity": 55.2,
  "voltage": 3.72,
  "rssi": -63,
  "battery": 86,
  "seq": 1024,
  "ts": 1762137600000
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| deviceId | string | 设备 ID |
| temperature | float | 温度 ℃，范围 -40 ~ 85 |
| humidity | float | 湿度 %RH，范围 0 ~ 100 |
| voltage | float | 电池电压 V，3.7V 锂电典型 3.0 ~ 4.2 |
| rssi | int | 信号强度 dBm，-120 ~ -30 |
| battery | int | 电量百分比 0 ~ 100 |
| seq | int | 设备本地自增序号，用于乱序/丢包检测 |
| ts | int | 设备采集时间戳，毫秒级 Unix 时间 |

### 2.2 状态 `device/{deviceId}/status`

```json
{
  "deviceId": "dev-0001",
  "status": "online",
  "firmwareVersion": "1.0.0",
  "reason": "boot",
  "ts": 1762137600000
}
```

- `status`：`online` / `offline`
- `reason`：`boot`（上电上线）、`sleep`（主动休眠下线）、`lwt`（Broker 遗嘱，设备异常掉线）
- 设备连接时以 **retained** 方式发布 `online`；遗嘱配置为同 Topic 的 `offline` 报文。

### 2.3 指令 `device/{deviceId}/cmd`

```json
{
  "cmdId": "cmd-8f1c2d",
  "type": "ota",
  "payload": { "targetVersion": "1.1.0", "url": "https://example.com/fw.bin", "size": 1048576 },
  "operator": "admin",
  "ts": 1762137600000
}
```

| type | payload | 说明 |
| --- | --- | --- |
| `calibrate` | `{ "temperatureOffset": 0.5, "humidityOffset": -1.0 }` | 传感器校准 |
| `ota` | `{ "targetVersion": "1.1.0", "url": "...", "size": 1048576 }` | 固件升级 |
| `reboot` | `{}` | 远程重启 |
| `config` | `{ "reportIntervalSec": 30, "sleepSec": 60 }` | 修改上报/休眠周期 |

### 2.4 指令回执 `device/{deviceId}/cmd/ack`

```json
{
  "cmdId": "cmd-8f1c2d",
  "type": "ota",
  "state": "progress",
  "progress": 45,
  "message": "下载固件中",
  "ts": 1762137600000
}
```

- `state`：`received` / `progress` / `success` / `failed`
- `progress`：0 ~ 100，仅 OTA 场景有值；`success` 回执即使未带 100，网关也按 100 记录
- **终态不可回退（v1.23.0）**：`success` / `failed` 一旦写入，后续乱序、重复或迟到回执
  不再改变状态与进度；OTA 任务的 `success` / `failed` 汇总随之固定。需要重试时创建新指令。
- **超时兜底（v1.23.0）**：指令超过 `COMMAND_TIMEOUT_SECONDS`（默认 300s）仍停在
  `issued` / `received` / `progress`，后台巡检将其置为 `failed`，`message` 写明超时秒数；
  任务不会再永久停在 running。阈值可通过环境变量覆盖，生产建议不小于单次 OTA 最长耗时。
- **归属校验（v1.23.0）**：回执只能推进 `device/{deviceId}/cmd` 对应设备的指令；
  跨设备回执被忽略并记 warning，避免设备 B 改写设备 A 的指令。

### 2.5 系统告警 `device/system/alert`

```json
{
  "alertId": "alt-3a91be",
  "deviceId": "dev-0001",
  "level": "critical",
  "metric": "voltage",
  "value": 2.91,
  "threshold": 3.2,
  "message": "设备 dev-0001 电池电压 2.91V 已低于安全阈值 3.2V，存在随时掉电风险，建议立即安排现场更换电池。",
  "aiGenerated": true,
  "matchedRule": {
    "rule_id": 12,
    "rule_name": "电压过低-严重",
    "op": "lt",
    "threshold": 3.2,
    "level": "critical",
    "value": 2.91,
    "consecutive": 1
  },
  "ts": 1762137600000
}
```

`matchedRule` 为 v1.34.0 附加字段：命中的规则快照（规则后续被修改/删除也不影响这条报文的解释）。
附加字段不破坏旧客户端解析；`device_alert.matched_rule` 与之一致，历史告警为 `null`。

### 2.6 设备分组变更 `device/system/group`（v1.14.0）

```json
{
  "deviceId": "dev-0001",
  "groupName": "二号车间",
  "previousGroupName": "一号车间",
  "operator": "admin",
  "ts": 1762243200000
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| deviceId | string | 设备 ID |
| groupName | string | 变更后的分组名；空串表示「未分组」 |
| previousGroupName | string | 变更前的分组名；空串表示原为「未分组」 |
| operator | string | 触发变更的账号 |
| ts | int | 事件时间，毫秒级 Unix 时间 |

- 仅在分组**真正变化**时发布；重复提交同一分组、只改名称等编辑不产生事件；
- 网关在数据库提交成功后发布，订阅端收到事件后立即回查 REST 一定能读到新分组；
- 发布为「最佳努力」：MQTT 不可用时只记录告警，`PATCH /api/devices/{deviceId}` 仍返回 200，
  客户端在下次刷新时从 REST 看到新分组（与 v1.13.x 的编辑语义一致）。

---

## 3. REST API（Python 网关，前缀 `/api`）

统一响应体：

```json
{ "code": 0, "message": "ok", "data": {} }
```

`code != 0` 表示业务失败，HTTP 状态码同时给出（400/401/404/429/500）。

### 3.1 认证

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | 登录，入参 `{username, password}`，返回 `{token, refresh_token, token_type:"bearer", expires_in, user:{username, role}}` |
| POST | `/api/auth/refresh` | 用 refresh 换新 access，入参 `{refresh_token}`，响应结构同登录；**轮换**：旧 refresh 立即失效 |
| POST | `/api/auth/logout` | 吊销当前 refresh（幂等），入参 `{refresh_token}`，返回 `{revoked: bool}`；不要求 access，access 已过期也能登出 |
| POST | `/api/users/{username}/sessions/revoke` | 管理员吊销该用户全部会话，返回 `{username, revoked}`（v1.24.0） |
| GET | `/api/auth/me` | 返回数据库中的最新账号信息（角色被调整后前端刷新即可生效） |
| GET | `/api/auth/mqtt-credentials?client=web\|mobile` | 登录后换取客户端只读 MQTT 凭证（v1.17.0） |

**令牌生命周期（v1.24.0 / P2-4）**

- access：无状态 JWT（HS256），TTL 由 `ACCESS_TOKEN_TTL_SECONDS` 控制，默认 **1200 秒（20 分钟）**；
- refresh：随机 48 字节 urlsafe 明文，TTL 由 `REFRESH_TOKEN_TTL_DAYS` 控制，默认 **14 天**；
  服务端**只存 SHA-256 指纹**（`refresh_token.token_hash` 为主键），明文仅在签发响应中出现一次；
- **单次使用 + 轮换**：每次 refresh 都签发新 refresh 并把旧行标记为 `rotated`；
  同一旧 refresh 第二次使用返回 401，并视为令牌泄露，**吊销该用户全部会话**（审计 `auth.refresh_reuse`）；
  并发竞争下只有一方能原子占用旧令牌，竞争失败同样按重放处理（宁严勿松）；
- **吊销范围**：登出只吊销当次 refresh；管理员吊销、改密、删除账号会吊销该用户全部 refresh；
- **必须注意的残留窗口**：吊销只作用于 refresh。**已签发的 access 在自身 `exp` 前仍然可用**，
  最长 `ACCESS_TOKEN_TTL_SECONDS`（默认 20 分钟）。本版本不宣称「吊销立即全局生效」；
  缩小窗口的手段是压短 access TTL（代价：刷新频率与网关 DB 写入增加），
  **没有**引入每请求黑名单校验（代价：每次 API 调用都要查 Redis/DB，破坏无状态校验并放大故障面）。
  需要分钟级强下线时，把 `ACCESS_TOKEN_TTL_SECONDS` 调到 300~900 并接受相应的刷新开销。

| 场景 | 状态码 | 响应 detail / 说明 |
| --- | --- | --- |
| 登录成功 | 200 | 签发 access + refresh，`expires_in` 为 access 秒数 |
| 账号/口令错误、账号停用 | 401 | `用户名或密码错误` |
| refresh 不存在 | 401 | `刷新令牌无效` |
| refresh 已过期 | 401 | `刷新令牌已过期`（同时把该行标记 `expired`） |
| refresh 已登出/改密/管理员吊销 | 401 | `刷新令牌已被吊销` |
| 旧 refresh 被重放 | 401 | `刷新令牌已被使用，已吊销该账号全部会话` |
| refresh 对应的账号被停用/删除 | 401 | `账号不存在或已停用`，并吊销该用户全部会话 |
| 登出（无论令牌是否有效） | 200 | `{revoked: true/false}`，重复登出不报错 |

客户端约定（Web 与移动端一致）：401 时先尝试一次 refresh 并重放原请求；
refresh 失败则清空本地 access/refresh 并回登录页。并发 401 必须**单飞**（一个进程/页面只发一次 refresh），
Web 用模块级 Promise，移动端用 `SessionRefreshCoordinator`，详见 3.10。

后续请求头：`Authorization: Bearer <token>`。登录凭证校验数据库 `app_user` 表，
口令以 PBKDF2-SHA256 哈希存储。角色与权限矩阵：

| 角色 | 权限 |
| --- | --- |
| `admin` | 全部：设备/告警/OTA/审计查询 + 阈值配置 + 账号管理 |
| `operator` | 查询 + 下发指令 + 创建 OTA 任务 + 确认告警 |
| `viewer` | 只读：仅设备/告警/OTA/审计查询 |

越权访问返回 HTTP 403，未登录或令牌失效返回 401。

**登录失败限流（v1.16.0 新增）**：按「直连客户端 IP + 用户名」在进程内计数，
窗口内失败达到 `LOGIN_MAX_FAILURES`（默认 5）后锁定 `LOGIN_LOCKOUT_SECONDS`（默认 900 秒）：

| 场景 | 状态码 | 响应 |
| --- | --- | --- |
| 账号/口令错误、账号停用 | 401 | `{code:401, message:"用户名或密码错误", data:null}` |
| 窗口内失败次数达到阈值后的再次尝试 | 429 | 同结构；响应头 `Retry-After: <剩余秒数>` |
| 成功登录 | 200 | 原有 `{token, user}` 结构不变；该 IP+账号计数清零 |

限流状态为**进程内内存**实现：多进程/多副本之间不共享，重启清零；生产需要全局一致时
应在反向代理层（如 nginx `limit_req`）或 Redis 中实现。网关默认不读取 `X-Forwarded-For`。

**口令强度（v1.16.0）**：`POST /api/users` 与 `PATCH /api/users/{username}` 新写入的口令
至少 `PASSWORD_MIN_LENGTH`（默认 8）位，且拒绝常见弱口令；不合规返回 400 与可读文案。
既有账号的口令不做回溯校验，继续可登录。

**客户端 MQTT 凭证（v1.17.0）**：`GET /api/auth/mqtt-credentials?client=web|mobile`
需登录（任意角色），返回：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "client": "web",
    "username": "web-console",
    "password": "<服务端生成的只读口令>",
    "read_only": true,
    "topics": ["device/+/sensor/report", "device/+/status", "device/+/cmd/ack",
               "device/system/alert", "device/system/group"]
  }
}
```

- 口令来自服务端环境变量 `MQTT_WEB_PASSWORD` / `MQTT_MOBILE_PASSWORD`，**不进入前端构建产物**；
- 未配置口令返回 503；`client` 仅接受 `web` / `mobile`，其他值 422；
- Web 与移动端使用各自独立账号，9001 监听的 ACL 只有 read 规则，无任何 write；
- 客户端实现：Web 登录后先取凭证再 `mqtt.connect`，取不到时保持未连接并重试，**不匿名降级**；
  移动端在实时服务启动前调用同一接口，失败则提示并降级 REST 轮询。

### 3.2 设备

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/devices` | 列表，Query：`status`(online/offline)、`keyword`、`group`、`tag`（v1.35.0，可重复）、`hasAlert`、`withAlertCount`、`page`、`pageSize` |
| GET | `/api/devices/groups` | 分组概览：每组 `total` / `online` / `offline` |
| GET | `/api/devices/tags` | v1.35.0：标签列表与设备数 `[{tag, device_count}]`，登录可读 |
| POST | `/api/devices/tags/batch` | v1.35.0：批量打/移除标签（仅 admin），入参 `{device_ids, add?, remove?}`，逐设备返回结果 |
| POST | `/api/devices` | 手工注册设备，入参 `device_id`（3~64 位字母数字 _-）、`name`、`model`、`firmware_version`、`group_name`；重复 ID 返回 409 |
| POST | `/api/devices/bulk` | 批量注册，单次最多 200 台，返回 `{created, skipped}`，重复 ID 跳过 |
| POST | `/api/devices/{deviceId}/mqtt-credential` | 签发/轮换设备 MQTT 凭证：首次签发 operator+admin，轮换已有有效凭证仅 admin（v1.26.0 收紧） |
| DELETE | `/api/devices/{deviceId}/mqtt-credential` | 吊销设备凭证（仅 admin，v1.26.0）；已吊销/未签发返回 409 |
| PATCH | `/api/devices/{deviceId}` | 编辑名称 / 型号 / 固件版本 / 分组；设备 ID 与状态不可改 |
| DELETE | `/api/devices/{deviceId}` | 删除设备（仅 admin）；存在历史时序数据时返回 409 |
| GET | `/api/devices/{deviceId}` | 详情 + 最新一条传感器数据 |
| GET | `/api/devices/{deviceId}/shadow` | v1.36.0：设备影子（期望/上报/差异/同步状态），登录可读 |
| PUT | `/api/devices/{deviceId}/shadow/desired` | v1.36.0：merge 写入期望，`{patch}`，operator/admin |
| DELETE | `/api/devices/{deviceId}/shadow/desired` | v1.36.0：清除期望键，重复 Query `keys`，operator/admin |
| POST | `/api/devices/{deviceId}/shadow/reported` | v1.36.0：上报实际状态，`{reported, version}`，operator/admin |
| POST | `/api/devices/shadow/desired/batch` | v1.36.0：按设备/标签/分组批量写期望，operator/admin |
| GET | `/api/devices/{deviceId}/shadow/history` | v1.37.0：不可变期望快照，Query `limit`(1~100，默认 50)、`offset`(默认 0)，登录可读 |
| POST | `/api/devices/{deviceId}/shadow/rollback` | v1.37.0：完整快照替换回滚，入参 `{version}`，operator/admin |
| GET | `/api/devices/{deviceId}/records` | 历史数据，Query：`start`、`end`（ISO8601 或毫秒时间戳）、`limit`、`offset`（v1.13.0） |
| GET | `/api/devices/{deviceId}/records/aggregate` | 时序聚合，Query：`bucket`(hour/day)、`start`、`end`，返回每桶 count 与三指标 avg/min/max |
| GET | `/api/records/latest` | 全局最新数据（仪表盘首屏兜底），Query：`status`(online/offline)、`keyword`、`limit`(1~500)、`offset`、`deviceIds`，均可选 |

`/api/records/latest` 的过滤与分页参数全部可选：不传时语义与历史一致（每个设备各自最新一条、
按时间倒序、不截断）；`keyword` 同时匹配设备 ID 与名称，`status` 依赖设备表关联。
`deviceIds` 为逗号分隔的设备 ID（单次最多 200 个，自动去重），只返回这些设备的最新值；
传空串返回空数组，便于看板按已加载页分批取值，不再全量拉取。

`/api/devices` 的 `status` / `keyword` / `group` / `tag` / `page` / `pageSize` 同样为可选参数，
不传时默认第一页 20 台、不加过滤。v1.11.0 新增两个可选参数，默认关闭、不影响既有调用方：

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| `hasAlert` | `true` / `false`（默认） | 仅返回存在未处理告警的设备；筛选在数据库层按 `device_alert.status='open'` 聚合，不依赖告警列表分页大小 |
| `withAlertCount` | `true` / `false`（默认） | 每个设备条目额外返回 `open_alert_count`（未处理告警数，无告警为 0）；不传时该字段为 `null`，既有字段语义不变 |

设备条目（`DeviceOut`）字段：`device_id`、`name`、`model`、`firmware_version`、`group_name`、
`status`、`last_report_at`、`last_online_at`、`tags`（v1.35.0，字符串数组，无标签为 `[]`）、
`open_alert_count`（可选，仅 `withAlertCount=true` 时填充）。
v1.26.0 起额外返回凭证元数据：`mqtt_username`、`mqtt_credential_version`、
`mqtt_credential_issued_at`、`mqtt_credential_rotated_at`、`mqtt_credential_revoked_at`、
`mqtt_credential_status`（`missing` / `active` / `revoked`）、
`mqtt_credential_fingerprint_prefix`（**仅指纹前 8 位**）。列表与详情一致，均不返回口令或完整指纹。

v1.12.0 起 `group` 参数支持「未分组」：传 `group=未分组` 时匹配 `group_name` 为空串或字面「未分组」的设备；
`GET /api/devices/groups` 也把空分组名与「未分组」合并为同一个 `未分组` 桶（`total` / `online` / `offline` 累加）。
不传 `group` 时行为不变；普通组名筛选语义不变。

**设备标签筛选（v1.35.0）**：`tag` 是可重复查询参数（`?tag=冷库&tag=关键`），多个标签之间为
**「或」**：设备持有其中任意一个标签即命中。`tag` 与 `status` / `keyword` / `group` / `hasAlert`
同时存在时为**「且」**组合（标签是设备集合过滤，其余条件继续叠加）。不传 `tag` 时生成与升级前
完全相同的 SQL（有回归测试断言），列表总数与顺序均不变。

**标签模型与约束（v1.35.0）**：设备标签用独立关联表 `device_tag`，不是把逗号塞进
`device.group_name` 或设备表某字段：

| 约束 | 值 | 说明 |
| --- | --- | --- |
| 存储结构 | `device_tag(device_id, tag, created_at, created_by)` | 复合主键 `(device_id, tag)` 在数据库层强制唯一；同一设备同一标签不会重复 |
| 索引 | `(tag, device_id)` | 按标签筛选设备走索引；按设备取标签走主键前缀 |
| 单标签长度 | 32 | 超出返回 400，文案指出具体标签 |
| 单设备标签数 | 16 | 超出时该设备整台跳过并标记 `over_limit`，不会「加一半」 |
| 单次请求设备数 | 200 | 与批量注册口径一致 |
| 单次请求标签数 | 50 | 防止一次写爆 |
| 字符集 | `A-Za-z0-9_-` 与中文 | 空、纯空白、超长、非法字符均返回 400 可读原因 |

选择关联表而不是数组列的理由：SQLite（测试）与 PostgreSQL（生产）都能用同一套 SQL 与索引；
数组列在 SQLite 只能存 JSON 文本、无法建普通索引，且重复标签无法在数据库层约束。
关联表的代价是打标签需要多一次插入/删除，但设备量级与标签数量都有限，可接受。

**批量打标签 `POST /api/devices/tags/batch`（仅 admin）**：

- 入参：`{"device_ids": ["VG-...", ...], "add": ["冷库"], "remove": ["旧标签"]}`，
  `add` / `remove` 至少一个非空；同一标签同时出现在两者时以 `remove` 后 `add` 的顺序执行为「净增」。
- 逐设备返回 `status`：`updated`（有实际增删）、`unchanged`（标签已存在/本就没有可移除标签）、
  `not_found`（设备不存在）、`over_limit`（执行后标签数会超过 16，整台跳过）。
- 响应同时给出汇总数 `updated` / `unchanged` / `not_found` / `over_limit` / `added` / `removed`，
  调用方必须逐类展示；**不会**因为个别失败而整体失败或静默返回 200 成功。
- 写操作审计 `device.tags_update`，记录设备数与标签集合，不记录设备口令等敏感字段。
- `GET /api/devices/tags` 登录即可读，返回每个标签下的设备数，按设备数倒序；用于 Web/移动端筛选。

**设备影子（v1.36.0）**：一行一设备，保存云端期望 `desired` 与设备实际上报 `reported`，
响应中的差异和同步状态由服务端统一派生：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "device_id": "dev-0001",
    "desired": {"report_interval": 30, "led": true},
    "reported": {"report_interval": 10, "led": true},
    "version": 7,
    "reported_version": 6,
    "sync_status": "pending",
    "diff_keys": ["report_interval"],
    "pending_desired": {"report_interval": 30},
    "desired_updated_at": "2026-11-13T02:00:00+00:00",
    "desired_updated_by": "operator",
    "reported_updated_at": "2026-11-13T02:01:00+00:00"
  }
}
```

| 状态 | 判定 |
| --- | --- |
| `empty` | `desired={}`；即使 `reported` 留有自由状态键，也不视为待同步 |
| `synced` | `desired` 非空，且所有期望键均与 `reported` 同值 |
| `pending` | 设备在线，且至少一个期望键缺失或值不同 |
| `offline_pending` | 设备离线，且至少一个期望键缺失或值不同；设备上线后主动拉取 |

- `diff_keys` 只检查 `desired` 中的键；`reported` 额外拥有的键不构成差异。
  `pending_desired` 是这些差异键对应的当前期望值，客户端不得在本地另造一套判定。
- `PUT desired` 入参 `{"patch":{"report_interval":30,"led":null}}` 使用 merge patch：
  普通值新增/覆盖，`null` 删除对应期望键。`DELETE desired?keys=led&keys=fan_speed`
  是等价的显式清键入口，`keys` 为空返回 400。
- 只有 patch 真正改变 `desired` 才令 `version +1`；同值覆盖、删除不存在的键均幂等，
  不递增版本、不重复写审计。空 patch 返回 422，非法键/值返回 400。
- `POST reported` 入参 `{"reported":{"report_interval":30},"version":7}` 同样按字段 merge，
  `null` 清除对应上报键。`reported_version` 只前进、不随乱序旧回执后退，并夹紧到当前
  `version`；同步状态仍以实际字段差异为准，不只看版本号。
- 单个 patch/reported 最多 32 键；键长 1~32；值只允许字符串、数字、布尔、`null`，
  不允许数组或嵌套对象；单值 JSON 序列化长度上限 512。
- 所有登录角色可 GET；单台/批量写 `desired` 及 POST `reported` 均为 operator/admin，
  viewer 写入 403，未登录 401，设备不存在 404。

**期望版本历史与精确回滚（v1.37.0）**：

`GET /api/devices/{deviceId}/shadow/history?limit=50&offset=0` 按 `version` 倒序返回完整、
不可变快照；`total` 是不受分页影响的总数，`items` 单条结构如下：

```json
{
  "id": 4,
  "device_id": "dev-0001",
  "version": 8,
  "desired": {"report_interval": 30},
  "changed_keys": ["led", "report_interval"],
  "action": "rollback",
  "source_version": 5,
  "created_at": "2026-11-13T03:00:00+00:00",
  "created_by": "operator"
}
```

- `action` 为 `baseline / merge / clear / batch / rollback`；`changed_keys` 包含相对前一状态的
  新增、删除和值变化键，`source_version` 仅 rollback 记录目标版本，其余为 `null`。
- merge、显式清键、批量修改和回滚只在期望真实变化后保存快照；同值写入不空增版本或历史。
- `POST /api/devices/{deviceId}/shadow/rollback` 入参 `{"version":5}`，响应仍为 `DeviceShadowOut`。
  回滚是**完整快照替换**，不是 merge：目标快照没有的当前键会移除；生成当前版本 +1 的
  `rollback` 快照，不覆盖目标版本、不修改旧历史。
- 当前 `desired` 已与目标快照一致时幂等返回，不递增版本、不新增历史或审计；目标快照不存在返回 404。
- 回滚不修改 `reported` / `reported_version`。设备下次唤醒仍走 REST GET → 应用 → POST reported；
  对完整快照移除的旧上报键必须显式上报 `null`，才能从 merge 后的 reported 中删除。
- 历史 GET 所有登录角色可读，回滚仅 operator/admin；未登录 401、viewer 回滚 403。
- 回滚审计动作 `device.shadow_rollback`，只记录 `sourceVersion`、新版本、变化键与 changed，
  不记录期望状态值。历史表按功能需要保存完整 `desired` 快照。
- 迁移 `0012_shadow_history` 只为升级时的当前影子各补一条 `baseline`，不会臆造此前未保存的版本。

**批量期望 `POST /api/devices/shadow/desired/batch`**：

```json
{
  "device_ids": ["dev-0001", "dev-0002"],
  "tags": ["冷库"],
  "group": "华东",
  "patch": {"report_interval": 30}
}
```

- `device_ids` / `tags` / `group` 至少提供一种；同时提供时取交集。标签多值为「或」，
  与分组、显式 ID 为「且」，复用 v1.35.0 的设备筛选口径。
- 显式 ID 最多 200 个，标签最多 16 个，筛选后最多 200 台。被标签/分组条件排除的现存设备
  不进入 `matched` 与 `results`，**不会误标为不存在**；`not_found` 只统计显式 ID 中真正不存在的设备。
- 逐台返回 `updated` / `unchanged` / `not_found`、`version` 与 `error`，并汇总
  `matched/updated/unchanged/not_found`；部分不存在不回滚其他设备。
- 单台写审计 `device.shadow_desired_update`，批量有实际变更时写一条
  `device.shadow_desired_batch`；只记录键名、版本、筛选与数量，**不记录期望状态值**。
- 当前版本设备侧链路为**唤醒后 REST 主动 GET desired、应用后 POST reported**；
  未新增 shadow MQTT Topic，也不宣称网关会实时推送期望。离线期望等待下一次上线/唤醒。

写操作权限：注册 / 批量注册 / 编辑 / **首次签发**设备凭证为 operator+admin；
**轮换已有有效凭证**与**吊销凭证**仅 admin（会造成设备断线，v1.26.0 起收紧）；删除为 admin。

**设备 MQTT 凭证（v1.17.0）**：`POST /api/devices/{deviceId}/mqtt-credential`

- 用户名恒等于 `device_id`，凭证与 `deviceId` 强绑定。设备从未签发或已吊销时调用为
  **首次签发**（`operation=issue`，operator 可用）；已有有效凭证时调用为**轮换**
  （`operation=rotate`，仅 admin）。设备注册后、首次连接 Broker 前必须调用一次。
- 响应中的 `password` **只在本次响应出现一次**，平台不提供再次查看；请立即写入设备的安全存储。
  ```json
  {
    "code": 0,
    "message": "ok",
    "data": {
      "device_id": "dev-0001",
      "username": "dev-0001",
      "password": "<仅本次返回的随机口令>",
      "version": 2,
      "issued_at": "2026-11-05T10:00:00+00:00",
      "rotated_at": "2026-11-05T10:00:00+00:00",
      "operation": "rotate",
      "reloaded": true,
      "reload_detail": "已向 Mosquitto（pid=12345）发送 SIGHUP"
    }
  }
  ```
- 存储位置：Broker 的 `password_file` 只保存 PBKDF2-SHA512 hash；`device` 表只保存
  `mqtt_username`、`mqtt_credential_version`、SHA-256 `fingerprint`、签发/轮换/吊销时间，
  **不保存明文口令**。`DeviceOut` 只返回 8 位指纹前缀，不返回完整 fingerprint 与口令。
- `reloaded=false` 表示口令/吊销已写入但 Broker 未重载；**旧凭证可能仍然有效**，
  `reload_detail` 给出原因。接口仍返回 200 与本次口令（否则调用方拿不到已写入的新凭证），
  但调用方必须按失败处理并在界面明示；补救见 `docs/operations.md`。
- 注意：Broker 重载密码文件不会强制断开已建立的 MQTT 会话。旧凭证对新连接立即失效，
  但已连接设备可能维持到下次断线重连，因此轮换后设备可能短暂仍显示在线。
- 写密码文件失败返回 503；设备不存在返回 404；未登录 401、viewer 403；
  operator 尝试轮换返回 403。
- 审计：`device.credential_issue` 记录 `operation`、`version`、`reloaded`、`reload_detail`、
  `result`（`reload_ok` / `reload_failed`），不只记录动作。

**设备 MQTT 凭证吊销（v1.26.0）**：`DELETE /api/devices/{deviceId}/mqtt-credential`（仅 admin）

- 从 Broker `password_file` 删除该设备账号，并写入 `mqtt_credential_revoked_at`；
  已吊销或未签发返回 409，设备不存在返回 404。
- 响应不含口令与完整指纹：
  ```json
  {
    "code": 0,
    "message": "ok",
    "data": {
      "device_id": "dev-0001",
      "username": "dev-0001",
      "version": 2,
      "revoked_at": "2026-11-05T10:05:00+00:00",
      "reloaded": true,
      "reload_detail": "已向 Mosquitto（pid=12345）发送 SIGHUP"
    }
  }
  ```
- `reloaded=false` 同样表示 Broker 未重载、旧凭证可能仍有效；审计动作
  `device.credential_revoke` 记录 `result` 与 `reload_detail`。
- 吊销后设备必须重新签发（`POST`，此时为 `operation=issue`）才能接入。

v1.14.0 起 `PATCH /api/devices/{deviceId}` 是 Web 后台改分组的唯一入口（后台没有批量改分组），
分组变化时网关额外发布 2.6 的 `device/system/group` 事件；接口入参、响应与错误语义不变，
事件仅在 MQTT 可用时产生，旧客户端不订阅该 Topic 即保持原有行为。

### 3.3 告警

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/alerts` | 列表，Query：`deviceId`、`level`(warning/critical)、`status`(open/acked)、`sla`（v1.33.0：met/late/pending/overdue/unknown）、`tag`（v1.35.0，可重复，或语义）、`start`/`end`（v1.30.0，`created_at` 闭区间）、`page`(≥1)、`pageSize`(1~200)；`total` 为过滤后总数 |
| POST | `/api/alerts/bulk-ack` | 批量确认（v1.15.0 新增，v1.21.0 支持备注），见 3.3.1 |
| POST | `/api/alerts/{alertId}/ack` | 单条告警确认（v1.21.0 支持备注，v1.33.0 返回 SLA 状态），见 3.3.2 |
| GET | `/api/alerts/summary` | 告警统计：`{open, critical_open, today, overdue_open}`（v1.33.0 增 `overdue_open`） |
| GET | `/api/alerts/sla-config` | 读取 SLA 配置（登录即可读） |
| PUT | `/api/alerts/sla-config` | 修改 SLA 配置（仅 `admin`），写审计 `sla.config_update` |
| POST | `/api/alerts/handover` | 值班交接（`operator`/`admin`），见 3.3.4 |

**级别口径（P2-1 / v1.21.0）**：规则引擎按阈值真实产出的级别只有 `warning` 与
`critical` 两级；对外枚举同步收敛，`level` 传 `info` / `notice` 一律 422。
Web 与移动端不再展示永远为空的级别，历史数据也不存在这两级（规则从未产出）。

**处置元数据（P2-2 / v1.21.0）**：`AlertOut` 新增三个可选字段，回答「谁、何时、为什么确认」：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `acked_by` | string \| null | 确认人（网关登录用户名）；历史确认行为 `null` |
| `acked_at` | ISO8601 \| null | 确认时间（UTC）；历史确认行为 `null` |
| `ack_note` | string \| null | 处置备注（≤500 字，首尾空白已去除）；未填写为 `null` |

v1.21.0 之前已确认的历史行**不做臆测回填**：无法得知真实操作人与时间，
页面显示「历史确认（无处置记录）」。重复确认（`already_acked`）不覆盖首次
`acked_by` / `acked_at` / `ack_note`，保证处置证据稳定。

**命中规则（v1.34.0）**：`AlertOut` 新增可选 `matched_rule`，回答「为什么触发」：

```json
{
  "rule_id": 12, "rule_name": "电压过低-严重", "op": "lt",
  "threshold": 3.2, "level": "critical", "value": 2.91, "consecutive": 1
}
```

规则后续被修改或删除都不改写该快照；v1.34.0 之前的历史告警为 `null`，
Web 显示「默认阈值（历史）」。裁决口径见 3.4。

#### 3.3.1 批量确认 `POST /api/alerts/bulk-ack`（v1.15.0；v1.21.0 增备注）

| 项 | 契约 |
| --- | --- |
| 权限 | `operator` / `admin`；`viewer` 403，未登录 401（与单条确认一致） |
| 入参 | `{"alert_ids": ["alt-..."], "note": "可选备注"}`，`alert_ids` 去重前 1~100 条；`note` 可省略、≤500 字；空列表/超限/备注超长 422 |
| 出参 | `data: {acked: [], already_acked: [], not_found: [], requested: N}`；`requested` 为去重后条数 |
| 部分失败 | 不整批回滚、不返回 404：open → acked 计入 `acked`；已是 acked 幂等计入 `already_acked`；不存在的 ID 计入 `not_found`，调用方逐类展示 |
| 全失败 | 全部 ID 不存在时仍返回 200，`acked` 为空、`not_found` 为全部去重后的 ID |
| 处置写入 | 本批 open → acked 的行写同一份 `acked_by` / `acked_at` / `ack_note`；已 acked 行保持不变 |
| 事务与审计 | 单事务提交；写一条 `alert.ack` 审计，`detail` 记录 `bulk` / 请求数与三类计数，`note_summary` 为脱敏后前 100 字（不带原始敏感内容） |
| 兼容 | 旧客户端不传 `note` 照常工作；批量端点纯新增，旧客户端不受影响 |

#### 3.3.2 单条确认 `POST /api/alerts/{alertId}/ack`（v1.15.0；v1.21.0 增备注）

| 项 | 契约 |
| --- | --- |
| 权限 | `operator` / `admin`；`viewer` 403，未登录 401 |
| 入参 | 可选 body `{"note": "..."}`（≤500 字）；**不传 body 的旧客户端照常工作** |
| 出参 | `data: AlertOut`，`status=acked` 且带 `acked_by` / `acked_at` / `ack_note` |
| 幂等 | 已 acked 的行再次确认返回 200，保留首次处置信息（不覆盖） |
| 错误 | 告警不存在 404；备注超长 422 |
| 审计 | 写 `alert.ack`，`detail` 含 `deviceId` / `level` / `metric` / `note_summary`（脱敏摘要） |

#### 3.3.3 SLA 口径与超时升级（v1.33.0）

`AlertOut` 在 v1.33.0 新增以下字段（列表、确认、详情同口径，均由网关计算后才返回）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `assignee` | string \| null | 责任人；未指派为 `null`，交接后为目标值班人 |
| `sla_minutes` | int | 创建时的时限快照（分钟） |
| `sla_deadline_at` | ISO8601 | 截止时间 = 创建时刻 + 快照时限 |
| `sla_state` | string | `met` / `late` / `pending` / `overdue` / `unknown` |
| `sla_remaining_seconds` | int \| null | 未确认时剩余秒数；已超时为负数；已确认为 `null` |
| `sla_response_seconds` | int \| null | 确认时与创建时的差值；未确认为 `null` |
| `escalation_count` | int | 已发出的升级次数 |

**三态判定（含历史行）**：

| `sla_state` | 条件 | 是否达标 |
| --- | --- | --- |
| `met` | `status=acked` 且 `acked_at` 存在且 `acked_at <= sla_deadline_at` | 是 |
| `late` | `status=acked` 且 `acked_at` 存在且 `acked_at > sla_deadline_at` | 是（超时达成） |
| `unknown` | `status=acked` 但 `acked_at` 为 `NULL`（v1.21.0 之前的历史确认行） | **否**，不计入达标 |
| `overdue` | `status=open` 且当前时间已过 `sla_deadline_at` | 否 |
| `pending` | `status=open` 且未到 `sla_deadline_at` | 否 |

历史已确认但 `acked_at` 为 `NULL` 的行**不臆测时间**，统一归为 `unknown`：
既不算达标也不算超时，筛选「达标」时不会出现，避免把无法证明的处置当成合规。

**按级别时限**：创建告警时按 `level` 取 `critical_minutes` / `warning_minutes`，写入
`sla_minutes` 与 `sla_deadline_at` 快照。之后改配置只影响新告警，不回改存量。

**升级策略：逐级升级（fixed-step）+ 上限**。第 N 次升级在
`sla_deadline_at + (N-1) * escalation_interval_minutes` 触发，`N` 从 1 开始；
`max_escalations` 为上限，达到后停止升级但 SLA 仍为 `overdue`（不洗白）。
选择逐级升级而非「首个超时点连续升级」，是为了让值班人有真实的处理间隔，
同时上限能防止长期无人处理时反复轰炸接收端。

**幂等**：巡检用条件更新原子领取名额（`WHERE escalation_count = N-1`），
同一告警同一级别只发一次；先落 `notification_delivery` 再提交，网关重启后续投，
不会重复也不会丢。**同一条告警同一次升级只发一次**由 `escalation_count` 保证。

**升级通知载荷**（复用 v1.31.0 规则链路与投递重试，`event = alert.escalation`）：

```json
{
  "event": "alert.escalation",
  "deliveryId": 1,
  "alert": {"alertId": "...", "deviceId": "...", "level": "critical", "..." : "原有告警字段"},
  "escalation": {
    "attempt": 1,
    "maxAttempts": 2,
    "slaMinutes": 30,
    "deadlineAt": "2026-11-05T13:22:05+00:00",
    "overdueMinutes": 12
  }
}
```

Webhook 以 JSON POST 发送；SMTP 主题含「超时升级 第N/M次」，正文含原 SLA 与超时时长。
无规则命中或通知总开关关闭时，投递记 `suppressed` 且不发送，可在投递历史看到原因。

#### 3.3.4 值班交接 `POST /api/alerts/handover`（v1.33.0）

| 项 | 契约 |
| --- | --- |
| 权限 | `operator` / `admin`；`viewer` 403，未登录 401 |
| 入参 | `{"to": "下一值班人", "from_user": "可选，默认当前登录人", "include_unassigned": true, "note": "可选备注 ≤500 字"}` |
| 范围 | `status != acked` 且（`assignee == from_user`，或 `include_unassigned` 为真时 `assignee IS NULL`）；**不会接管他人名下告警** |
| 出参 | `data: {from_user, to, count, window_start, window_end, max_level, alerts, notification}`；`notification` 为 `queued` / `skipped` |
| SLA | **不重置** `sla_deadline_at` 与 `escalation_count`：换班不应洗白已超时或重新触发升级 |
| 审计 | 一条 `alert.handover`，`detail` 含 `from` / `to` / `count` / `window_start` / `window_end` / `include_unassigned` / `note_summary` |
| 通知 | 给接收人**只发一条汇总**（`event = alert.handover`），不按条发送；复用 v1.31.0 规则链路，忽略分组/设备条件，只按级别与可用渠道 |
| 幂等 | 交接本身可重复调用；第二次范围为空时 `count=0`，仍写审计但不再发汇总 |

`alert.handover` 的 `handover` 块：`{fromUser, to, operator, count, windowStart, windowEnd, note, message}`。

### 3.4 告警规则（v1.34.0 起；v1.35.0 扩展多值范围与标签）与旧阈值接口兼容

规则 = 指标 + 比较方向 + 阈值 + 级别 + 生效范围 + 启用状态 + 连续次数 + 优先级。
v1.35.0 起「生效范围」支持标签，且分组/设备/标签都可以填**多个值**。
级别仍只有 `warning` / `critical`（沿用 v1.21.0 收窄口径）。

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/rules` | 登录可读 | 规则列表；`operator` / `viewer` 只读 |
| POST | `/api/rules` | admin | 新增规则；校验失败 400（中文可读原因） |
| PUT | `/api/rules/{ruleId}` | admin | 修改规则；只传需要变更的字段 |
| PATCH | `/api/rules/{ruleId}/enabled` | admin | 启停，入参 `{enabled: bool}`；审计 `alert_rule.enable/disable` |
| DELETE | `/api/rules/{ruleId}` | admin | 删除规则（不可恢复；需保留判定依据时优先停用） |
| GET | `/api/rules/{ruleId}/history` | 登录可读 | v1.35.0：变更历史（直接读 `audit_log`），Query `limit`(默认 50)，返回 `[{id, action, operator, created_at, fields, changes}]` |
| POST | `/api/rules/restore-defaults` | admin | 默认规则阈值回滚到环境变量/出厂值，自定义规则不动 |
| GET | `/api/rules/thresholds` | 登录可读 | **v1.33.0 兼容**：由默认规则推导的分组视图，每项 `{value, source}` |
| PUT | `/api/rules/thresholds` | admin | **v1.33.0 兼容**：只传待改键；同步写 `rule_config` 与默认规则 |

**规则字段**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string ≤64 | 规则名称，告警详情展示 |
| `metric` | `voltage` / `temperature` / `humidity` / `rssi` | 指标 |
| `op` | `lt` / `gt` | `lt`=低于阈值触发，`gt`=高于阈值触发；等于阈值**不**触发（与 v1.33.0 一致） |
| `threshold` | number | 阈值；范围 voltage 2~4.5、temperature -40~85、humidity 0~100、rssi -150~0 |
| `level` | `warning` / `critical` | 级别 |
| `scope_type` | `global` / `group` / `device` / `tag` / `custom` | 生效范围；v1.35.0 增 `tag`，v1.38.0 增 `custom`（条件树） |
| `scope_value` | string \| null | 兼容字段，保存范围**首个值**；`global` 时强制为 null，其余必填；条件树规则为镜像值 |
| `scope_values` | string[] \| null | v1.35.0 多值范围；`global` 为 null，其余至少 1 个、最多 50 个；条件树规则为镜像值 |
| `conditions` | object \| null | v1.38.0 条件树（all/any 嵌套，叶子为 group/device/tag）；null 表示走老字段语义 |
| `enabled` | bool | 停用后不参与判定，历史告警与统计不变 |
| `consecutive_count` | int 1~20 | 连续 N 次上报越限才触发；1 = 首次越限即触发 |
| `priority` | int 1~1000 | 同指标同级别多条命中时数字小者优先 |
| `is_default` | bool | 是否 v1.33.0 默认阈值的种子规则 |
| `updated_by` / `updated_at` | string / ISO8601 | 最近修改人与时间 |

**条件树（v1.38.0：可表达「且」）**

```json
{
  "op": "all",
  "children": [
    {"op": "any", "children": [
      {"dim": "tag", "values": ["冷库"]},
      {"dim": "tag", "values": ["关键"]}
    ]},
    {"dim": "group", "values": ["华东"]}
  ]
}
```

上面表示：设备持有「冷库」**或**「关键」标签，**且**属于分组「华东」。

1. **节点**：`{"op": "all" | "any", "children": [...]}`。`all` = 且（全部子条件成立）、
   `any` = 或（任一子条件成立）；两类节点可任意嵌套。
2. **叶子**：`{"dim": "group" | "device" | "tag", "values": [...]}`。**一个叶子内部多个值仍为「或」**
   （与 v1.35.0 完全一致）；不同维度用条件组表达「且」。
3. **限制与理由**：条件组层数 ≤3（含最外层）、叶子 ≤20、单叶子值 ≤50、单值 ≤64 字符；
   标签沿用 `device_tag` 的字符集与长度校验。深度限制覆盖「(A 或 B) 且 C」这一常见表达，
   再深会让界面与排障难以解释，也限制校验/求值开销。
4. **空条件树非法**：`children` 为空返回 400。**全局规则的唯一表达是
   `scope_type=global` 且 `conditions=null`**；不允许用空树表示「命中一切」，
   避免隐式放行。`conditions=null` 且 `scope_type` 为 group/device/tag 时按老字段匹配。
5. **优先级**：`conditions` 非 null 时它是唯一依据，老字段仅作降级镜像；
   请求同时给出条件树与不匹配的 `scope_type`（例如 `scope_type=group` 但树里还有标签）返回 400。
6. **与老字段的映射（兼容与回滚）**：
   - 单叶子条件树（`all`/`any` + 1 个叶子）写入时镜像为
     `scope_type=<dim>`、`scope_value=<首值>`、`scope_values=<全部值>`，
     v1.35 与旧客户端读到的范围完全等价；
   - 真正嵌套的树无法用单值字段表达，写入 `scope_type=custom`、`scope_value/scope_values=null`；
   - 迁移 `0013_rule_conditions` 把既有 group/device/tag 规则等价回填为单叶子 `any` 条件树，
     **不改写** `scope_type/scope_value/scope_values`。downgrade 到 v1.35 后老字段仍可读：
     单维度老规则行为不变；`custom` 规则在 v1.35 下不命中（fail-closed，不误报）。
7. **裁决口径不变**：条件树只改变「规则是否命中」，不改变下方「同指标取最高级别、
   同级按 priority 再 id、每指标一条告警」的裁决；规则之间仍是「或」+ 最高级别。
8. **审计**：`conditions` 变更写入 `changes.conditions = {from, to}`；单个字段值序列化后
   超过 2000 字符时存 `{truncated: true, chars, preview}`（前 2000 字符），
   保证一条 update 不会把 `audit_log.detail` 撑大；历史视图对截断值显示「内容过长已截断」。

**范围匹配语义（v1.35.0 老字段；仍适用于 `conditions=null` 的规则）**

1. **组内是「或」**：同一条规则内配置的多个分组/设备/标签之间为「或」。例如
   `scope_type=tag, scope_values=["冷库","关键"]` 表示设备持有「冷库」**或**「关键」任一标签即适用；
   不会要求同时持有两个标签。
2. **组间也是「或」**：同一指标的多条规则分别命中时，最终取最高级别（见下方裁决口径）。
3. **不同维度不混填**：老字段形态下 `scope_values` 只能属于同一种 `scope_type`；
   需要「分组 A **且** 标签 B」请使用 `conditions`。
4. 选择叶子内「或」而不是「且」的理由：标签是并列管理维度（如「冷库」「关键」），
   若按交集，多标签规则会趋近空集且界面无法解释「为什么没命中」；「或」与既有
   `hasAlert`/多值筛选的用户预期一致。需要交集时由条件组显式表达，语义可见、可验证。
5. 兼容：升级迁移 `0010_device_tags` 会把老规则的 `scope_value` 回填为
   `scope_values=[scope_value]`；匹配时优先读数组、数组为空再回退单值。已有单值规则与
   无标签设备的行为完全不变，downgrade 后 `scope_value` 仍是原值，可继续使用。

**方向与级别校验**：`voltage`/`rssi` 只允许 `lt`，`humidity` 只允许 `gt`，`temperature` 两者皆可；
同指标、同方向、同范围下 critical 必须比 warning 更严格（lt：critical < warning；gt：critical > warning），
反向配置返回 400；NaN/Inf、越界、分组/设备范围缺值同样 400。

**裁决口径（v1.34.0 选定：同指标取最高级别）**

1. 一条上报可同时命中同指标的多条规则（如电压 2.9V 同时低于 critical 3.2 与 warning 3.4）；
2. **取最高级别**：critical 胜 warning——安全语义不能被低优先级规则掩盖，也与 v1.33.0
   「critical 命中后不再叠加同指标 warning」的既有行为一致；
3. 同级多条命中时，按 `priority` 升序、再按 `id` 升序取第一条；
4. **每个指标最多产出一条告警**，保持既有抑制窗口、看板与移动端「一条告警一个指标」语义；
5. 连续次数按 `(设备, 规则)` 在进程内计数：越限 +1、未越限清零；网关重启清零，
   最多延迟 N-1 次上报（不建持久计数表，避免高频写库）。

**运行时生效**：规则全量缓存在网关进程内，每次上报读缓存；管理端写入后 write-through
重载缓存，无需重启。代价是单实例部署前提（多实例需各自刷新），MQTT 热路径不查库。

**审计与变更历史（v1.35.0 起记录前后值）**：
`alert_rule.create/update/enable/disable/delete/restore_defaults` 的 `detail` 保留
`name`、`rule_id` 与 `changed_fields`，并新增 `changes: {字段: {from, to}}`，用于
Web「规则变更历史」抽屉展示「何时、谁、改了哪些字段、前后值」。规则字段本身不含
MQTT 口令/密钥，因此阈值、范围、级别、启用状态可以记录前后值；审计不写入任何设备凭证。
历史接口 `GET /api/rules/{ruleId}/history` 直接过滤 `audit_log.target_type='alert_rule'`，
不新建平行记录表。删除规则时 `changes.deleted.from` 保存删除前快照、`to=null`，
便于事后解释「这条规则原本限制什么」。

**向后兼容**：升级时把 v1.33.0 的 10 个硬编码阈值（`rule_config` 在线覆盖优先，否则环境变量默认）
写成 `is_default=true` 的默认规则，范围 global、连续次数 1；因此同一份数据升级前后告警结果一致
（证据见 `gateway/tests/test_alert_rules.py::test_upgrade_behavior_matches_v133_samples` 与
`scripts/verify_alert_rules_v134.py`）。旧 `/api/rules/thresholds` 保存时同步写默认规则，
重启后不回退。v1.35.0 的迁移 `0010_device_tags` 只新增 `device_tag` 表与
`alert_rule.scope_values` 列，并回填 `[scope_value]`；不改变任何既有规则的阈值与裁决结果。
v1.38.0 的迁移 `0013_rule_conditions` 只新增 `alert_rule.conditions` 列，
并把 group/device/tag 规则等价回填为单叶子 `any` 条件树；老字段原样保留，
六种老形态（global / 单值 group / 单值 device / 多值 group / 多值 tag / 多值 device）
的判定结果逐条不变（证据：`gateway/tests/test_rule_conditions.py::test_legacy_scope_forms_match_identically_after_backfill`
与 `scripts/verify_conditions_v138.py`）。

### 3.4.1 维护窗口（v1.39.0）

维护窗口在**告警入库之前**拦截：窗口内命中规则的上报只写 `maintenance_suppression`，
不创建 `device_alert`，因此不发通知、不占 SLA、不进入列表/统计/角标。

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/maintenance/windows` | 登录可读 | 列表；`active` / `current_start` / `next_start` 按当前时间推导 |
| POST | `/api/maintenance/windows` | admin | 新增；非法范围/时间 400 |
| PATCH | `/api/maintenance/windows/{windowId}` | admin | 修改未结束窗口（部分字段） |
| POST | `/api/maintenance/windows/{windowId}/end` | admin | 提前结束，立即恢复告警并生成汇总 |
| GET | `/api/maintenance/windows/{windowId}/summaries` | 登录可读 | 每次窗口发生的汇总（含 `critical_suppressed`） |
| GET | `/api/maintenance/windows/{windowId}/suppressions` | 登录可读 | 被抑制上报记录，`page` / `pageSize` |

**语义**

- `scope` 复用告警规则条件树（`all`/`any` 嵌套，叶子 `group`/`device`/`tag`）；`scope=null`
  表示全部设备（显式全局，不用空树表达）。
- `mode=once`：`starts_at`/`ends_at`；`mode=weekly`：
  `recurrence={weekday, start:"HH:MM", duration_minutes}`，`weekday` 0=周一、6=周日。
- 时间一律 **UTC**，区间 **`[start, end)`**；相邻窗口不在边界重复计数。周期窗口
  跨天时延续到次日，`next_start` 在窗口内指向下一个周期。
- 重叠窗口都可命中，但同一上报只记到最早创建（id 最小）的窗口，汇总不重复计数。
- 窗口开始前已存在的 open 告警不受影响；窗口只拦截新的判定结果。
- 汇总只落站内（窗口 `summaries` 接口 + Web 展示），不额外发通知：计划内维护的
  汇总属于事后复盘信息，发通知会制造噪音；critical 压制条数在汇总中单独字段呈现。

### 3.5 审计日志

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/audit-logs` | 分页查询，Query：`operator`、`action`、`page`、`pageSize`；动作枚举：`login` / `command.create` / `command.bulk_create` / `ota.create` / `alert.ack` / `threshold.update` / `notify.config_update`（v1.29.0） / `notify.rule_create` / `notify.rule_update` / `notify.rule_delete`（v1.31.0） / `alert_rule.create/update/enable/disable/delete/restore_defaults`（v1.34.0） / `device.tags_update`（v1.35.0） |

### 3.6 告警通知通道（v1.29.0）

**链路位置**：MQTT 上报 → `alert_service` 判定 → `device_alert` 入库 → 发布
`device/system/alert`（App/Web 实时）→ **旁路异步通知**（Webhook / SMTP）。
通知是旁路：投递失败、重试、超时都不回滚告警，也不阻塞 MQTT 消费。

**配置热读取**：通道与规则存在 `notification_config`（固定 `id=1`），
每次告警派发时直接读库；Web 保存后**下一条告警立即生效，无需重启网关**。
未配置或 `enabled=false` 时完全不发通知（既有 E2E 默认即此状态）。

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/system/notifications/config` | admin | 读取配置；URL / 签名密钥 / SMTP 口令只返回 `*_configured` 布尔 |
| PATCH | `/api/system/notifications/config` | admin | 部分更新；未传字段保持原值，敏感字段传空串表示清空；写审计 `notify.config_update` |
| POST | `/api/system/notifications/test` | operator / admin | 真实发送测试通知，入参 `{channel: webhook \| smtp \| all}`，忽略规则与静默 |
| GET | `/api/system/notifications/deliveries` | admin | 最近投递记录，Query：`status=sent\|failed\|suppressed`、`page`、`page_size`（≤100） |

**配置字段（PATCH 入参）**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `enabled` | bool | 总开关 |
| `webhook_enabled` / `webhook_url` / `webhook_secret` | bool / str / str | Webhook 开关、地址、HMAC 密钥（可选） |
| `webhook_timeout_seconds` / `webhook_max_attempts` | int | 单次超时（1–60s）与最大尝试次数（1–10） |
| `smtp_enabled` / `smtp_host` / `smtp_port` | bool / str / int | SMTP 开关、服务器、端口 |
| `smtp_username` / `smtp_password` | str | 登录账号与口令（口令不回显） |
| `smtp_from` / `smtp_to` | str | 发件人；收件人支持逗号分隔多个 |
| `smtp_starttls` | bool | 是否 STARTTLS |
| `smtp_timeout_seconds` / `smtp_max_attempts` | int | 同 Webhook 边界 |
| `levels` | list | 触发级别，`warning` / `critical`，默认 `["critical"]` |
| `groups` | list | 设备分组白名单，空 = 全部分组 |
| `device_allow` / `device_deny` | list | 设备 ID 白名单/黑名单；**deny 优先**，空 = 不限 |
| `mute_minutes` | int | 静默窗口（0–1440 分钟），0 = 不去重 |
| `retry_backoff_seconds` | float | 指数退避基数：`base × 2^(attempt-1)` |

**规则语义（组合过滤）**：
`levels` **且** 设备分组命中（`groups` 为空视为全部）**且** 不在 `device_deny`
**且**（`device_allow` 为空或命中白名单）。任一不满足即不投递。
级别不匹配时连投递记录都不写（避免把「规则排除」误报成投递失败）。

**Webhook 请求**：

```http
POST <webhook_url>
Content-Type: application/json
X-Vikings-Event: alert            # 测试通知为 notification.test
X-Vikings-Test: 1                 # 仅测试通知
X-Vikings-Signature: sha256=<hex> # 配置了 webhook_secret 时
```

签名 = `HMAC-SHA256(secret, raw_body)`，十六进制小写，前缀 `sha256=`；
接收端必须对**原始字节**验签。Payload 结构：

```json
{
  "event": "alert",
  "sentAt": "2026-11-05T06:00:00+00:00",
  "alert": {"alertId": "alt-...", "deviceId": "dev-001", "level": "critical",
            "metric": "voltage", "value": 2.9, "threshold": 3.2, "message": "..."},
  "device": {"deviceId": "dev-001", "name": "设备名", "groupName": "默认分组", "status": "online"}
}
```

**SMTP 邮件**：主题 `[CRITICAL] 设备 <deviceId> <message>`（测试通知加 `[测试]`）；
正文为告警字段纯文本；支持 STARTTLS 与账号口令认证。
超时与重试口径与 Webhook 相同。

**重试与留痕**：每次通道投递独立记录到 `notification_delivery`：
`sent`（成功）、`failed`（重试耗尽）、`suppressed`（被静默窗口抑制，`channel=mute`）。
`error` 只记录可公开的错误类别（`HTTP <状态码>` / `请求超时` / `SMTP <异常类型>`），
**不包含 URL、主机、账号或口令**。

**安全**：GET 不回显 URL / 密钥 / 口令；PATCH 审计只记录变更字段名；
界面仅显示「已配置 / 未配置」。Webhook URL 与签名密钥、SMTP 口令不得出现在日志。

### 3.6.1 多规则路由、持久化重试与投递历史（v1.31.0）

**规则模型**：一份配置下可挂多条 `notification_rule`，每条规则独立决定接收人与匹配范围。
`notification_config` 保留总开关、静默窗口、退避基数与全局通道凭据（SMTP 服务器/口令等）；
规则只覆盖「发给谁」与「哪些告警」，避免每条规则重复存 SMTP 口令。

| 端点 | 权限 | 说明 |
| --- | --- | --- |
| GET `/api/system/notifications/rules` | admin | 按 `priority ASC, id ASC` 返回；敏感字段只返回 `*_configured` |
| POST `/api/system/notifications/rules` | admin | 创建规则；名称必填、`priority` 1–1000、每通道至少一个接收人；审计 `notify.rule_create` |
| PATCH `/api/system/notifications/rules/{id}` | admin | 部分更新；审计只写 `fields`（字段名），不写 URL/密钥/收件人值 |
| DELETE `/api/system/notifications/rules/{id}` | admin | 删除；审计 `notify.rule_delete`（ID + 名称） |
| POST `/api/system/notifications/test` | operator / admin | 可带 `rule_id` 指定规则；不带则用最高优先级可用规则；**忽略匹配与静默** |
| GET `/api/system/notifications/deliveries` | operator / admin | 投递历史；viewer 403（见下） |

**规则字段**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | str | 规则名（必填，界面展示） |
| `enabled` | bool | 停用后不参与裁决 |
| `priority` | int | **数字越小越优先**（1–1000），同值按 `id ASC` |
| `levels` / `groups` | list | 级别门槛；分组范围（空 = 全部分组） |
| `device_allow` / `device_deny` | list | 设备白/黑名单，`deny` 优先 |
| `webhook_enabled` / `webhook_url` / `webhook_secret` | bool / str / str | 本规则的 Webhook 接收人与 HMAC 密钥（URL/密钥不回显） |
| `smtp_enabled` / `smtp_to` | bool / str | 本规则的邮件接收人（逗号分隔；服务器与口令沿用全局配置） |

**优先级裁决**：按 `priority` 升序取**第一条**「已启用且同时满足级别、分组、设备白黑名单」的规则，
命中即用该规则的接收人；**不会**把多条规则叠加群发。规则按优先级在界面表格中自上而下展示，
同优先级按 ID 升序，便于值班预期一致。没有任何规则命中时**不投递**，写
`notification_delivery(status=suppressed, channel=rule, error="未匹配任何通知规则")`——
这样可以回答「这条告警为什么没发出去」，也避免全局兜底把未分类设备误发给默认值班人。
`notification_rule` 表为空（例如 v1.29.0 配置被手工清空）时运行期回退为 v1.29.0 的单配置行为。

**分组即时生效**：规则每次派发时按设备当前 `group_name` 判定，不做缓存；
设备改分组后，下一条告警立即按新分组命中规则（无需重启网关或重存规则）。

**持久化重试**：派发时先落 `notification_delivery(status=pending, next_attempt_at=...)`，
再由后台调度器（默认 5 秒一轮）原子领取到期行续投；网关重启后从表恢复。
第 `n` 次失败后下次时间 = `updated_at + retry_backoff_seconds × 2^(n-1)`，
达到通道 `max_attempts` 后置 `failed`、`next_attempt_at=NULL`，`error` 为脱敏原因。
`duration_ms` 为各次尝试耗时累计，`finished_at` 为终态时间。
**去重键**：每次投递的 `notification_delivery.id` 通过 `X-Vikings-Delivery-Id`
（Webhook）与确定性 `Message-ID`（SMTP）发给接收端；语义为 **at-least-once**，
接收端应按该 ID 幂等去重。网关侧以「租约 + 状态机」避免同一行被并发重复发送，
但进程在 HTTP 请求发出后、结果写回前崩溃时，重启后仍可能重发同一条（见运维 13.7）。

**投递历史端点**：

| Query | 说明 |
| --- | --- |
| `page` / `page_size` | 默认 1 / 20，`page_size ≤ 100`；返回 `{items, total, page, page_size}` |
| `status` | `pending` / `sent` / `failed` / `suppressed` |
| `channel` | `webhook` / `smtp` / `mute`（静默）/ `rule`（未匹配规则） |
| `device_id` | 精确匹配 |
| `start` / `end` | 按 `created_at` 过滤（ISO 时间，含边界） |

筛选与分页全部在服务端完成，Web 端不做二次过滤；排序固定 `created_at DESC, id DESC`。
历史页同时展示 `attempts`、`duration_ms`、`error`、`next_attempt_at`、规则名与分组。

**权限**：投递历史对 `operator` 与 `admin` 开放；`viewer` 返回 403。理由：`error` 会暴露
接收端基础设施线索（状态码、SMTP 异常类型、出网情况），属于运维信息；但值班人员
（operator）需要在不进系统设置的情况下确认「告警到底发没发出去」，因此不收紧到 admin。

### 3.7 指令 / OTA

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/devices/{deviceId}/commands` | 下发指令，入参 `{type, payload}`，网关于 MQTT 发布到 `cmd` Topic |
| GET | `/api/devices/{deviceId}/commands` | 指令历史（含回执状态/进度） |
| POST | `/api/devices/commands/bulk` | 批量指令（v1.28.0），入参 `{device_ids[], type, payload, request_id?}`；type ∈ `calibrate` / `config` / `reboot`，单次最多 200 台 |
| POST | `/api/ota/tasks` | 创建 OTA 任务，入参 `{deviceIds[], targetVersion, url, description}`（单次最多 200 台）；任一设备不存在返回 400 且不创建任务 |
| GET | `/api/ota/tasks` | OTA 任务列表（含每台设备升级状态） |
| GET | `/api/ota/tasks/{taskId}` | OTA 任务详情 |

**任务状态口径（v1.23.0）**：任务不单独存 `status` 字段，以
`total` / `success` / `failed` 与每台设备的 `state` / `progress` 表达进度：

| 情形 | 汇总 | 设备状态 |
| --- | --- | --- |
| 已下发、设备未回执 | `success=0, failed=0` | `issued` |
| 升级中 | `success=0, failed=0` | `received` / `progress` |
| 全部成功 | `success=total, failed=0` | `success`（progress=100） |
| 任一失败（含设备回执 failed、下发失败、超时） | `failed≥1` | `failed` |
| 部分成功 | `success + failed == total` | 各自终态 |

终态一经写入即稳定；`success`/`failed` 之和达到 `total` 时任务不再接受状态变化。

#### 3.7.1 批量指令 `POST /api/devices/commands/bulk`（v1.28.0）

**为什么新增端点**：前端循环 N 次单设备接口会产生 N 个并发请求、部分失败难以归类、
幂等与审计也落在 N 条记录上。批量端点由服务端串行预检与下发，一次审计、逐台结果。

请求：

```json
{
  "device_ids": ["dev-0001", "dev-0002"],
  "type": "config",
  "payload": {"reportIntervalSec": 30},
  "request_id": "batch-1700000000000-abc123"
}
```

- `device_ids`：1～200 台，重复 ID 在服务端去重；
- `type`：仅 `calibrate` / `config` / `reboot`。**`ota` 返回 422**——升级必须走
  `/api/ota/tasks`，需要任务级汇总、逐台进度与失败隔离；
- `request_id` 可选（8～64 字符）。同一操作人 10 分钟内重放同一 ID 返回首次结果
  （`replayed=true`，`batch_id` 与 `success` 不变），不会重复下发。当前网关单进程部署，
  幂等窗口存在进程内；未来多副本需迁移到数据库/Redis。

响应 `data`：

```json
{
  "batch_id": "cmdbatch-...",
  "type": "config",
  "requested": 2,
  "replayed": false,
  "success": [{"device_id": "dev-0001", "cmd_id": "cmd-..."}],
  "failed": [],
  "skipped": [{"device_id": "dev-0002", "reason": "设备离线，指令无法投递（MQTT 非持久会话）"}]
}
```

| 分类 | 含义 |
| --- | --- |
| `success` | 指令已落库并投递到 `device/{id}/cmd` |
| `failed` | 设备存在且预检通过，但下发失败（如 Broker 断开），原因逐台返回 |
| `skipped` | 预检跳过：设备不存在、设备离线（若 DB 无有效凭证元数据会补充说明） |

预检只跳过**离线**设备；在线设备必然已成功接入 Broker，即使 DB 中凭证元数据缺失
（历史/命令行签发）也不会误判。单设备端点保持原行为（不预检、离线也下发）。

权限 `operator` / `admin`（viewer 403）；每次非重放批次写一条
`command.bulk_create` 审计，含 batch_id、requestId、类型、成功/失败/跳过计数与抽样原因，
不逐台写 N 条审计。单台发布失败不中断整批。

### 3.8 统计 / 健康

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/stats/overview` | 设备总数/在线数/未处理告警数；v1.27.0 追加凭证缺口、告警级别分布、24 小时趋势与异常设备（见 3.8.1） |
| GET | `/api/system/status` | 系统状态快照（仅 admin）：`gateway` 运行时长、`mqtt` 连接与消费量、`storage` 容量、`scheduler` 后台任务参数、`notify` Webhook 是否启用（地址脱敏） |
| GET | `/api/health` | 存活探针：进程存活即 200，不查库/不查 Broker（无需鉴权） |
| GET | `/api/health/ready` | 就绪探针（v1.20.0，v1.23.0 加强）：真实读取 `device` 表 + MQTT 连接状态；未就绪返回 503 与各组件明细（无需鉴权） |
| GET | `/metrics` | Prometheus 文本指标（v1.20.0）：反代只允许内网来源，网关 8000 端口不对外发布 |

#### 3.8.1 概览聚合字段（v1.27.0）

`GET /api/stats/overview` **没有新增端点**，只在既有响应上追加字段，Web 指标卡与移动端概览页
共用这一套口径（机读字段均为 snake_case）：

| 字段 | 类型 | 说明 / 口径 |
| --- | --- | --- |
| `device_total` / `device_online` / `device_offline` | int | 与 `/api/devices` 的 `total` 同一数据源；在线 + 离线 = 总数 |
| `report_today` | int | 今日上报数（沿用既有定义，按 UTC 日切） |
| `alert_open` | int | 未处理告警数（`status=open`，与 `/api/alerts/summary` 的 `open` 一致） |
| `device_credential_missing` | int | 未签发有效 MQTT 凭证的设备数（`missing` / `revoked` 均计入） |
| `alert_warning_open` / `alert_critical_open` | int | 未处理告警按级别拆分；两者之和 = `alert_open`（v1.21.0 收窄口径后只含 warning/critical） |
| `trend` | `[{hour_start, reports, alerts}]` | 近 24 个整点小时桶，升序；`reports` 为窗口内上报条数，`alerts` 为窗口内**新增**告警条数（含已确认，按 `created_at`） |
| `trend_complete` | bool | 首条上报时间是否早于窗口起点；`false` 时客户端必须显示「数据不足 24 小时」，不得补假点 |
| `first_record_at` | datetime? | 全库最早一条上报时间；无数据为 `null` |
| `abnormal_devices` | `[{device_id, name, status, open_alert_count, reason}]` | 最多 10 台；`reason` ∈ `alert` / `offline` / `alert+offline`；排序：未处理告警数降序 → 离线优先 → `device_id` |

空桶返回 0（真实零值，不是缺失点）；`trend` 恒为 24 个元素。只读接口，viewer / operator / admin 均可访问。

#### 3.8.2 电池健康与续航（v1.32.0）

| 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/stats/battery-health` | `group?`（≤64 字符）、`days`（1–30，默认 7）、`limit`（1–100，默认 20） | 跨设备的电量风险分层、衰减趋势、分组汇总与续航风险清单 |

响应字段（`data`）：

| 字段 | 类型 | 说明 / 口径 |
| --- | --- | --- |
| `device_total` / `battery_known` | int | 筛选范围内设备数 / 窗口内有上报的设备数 |
| `critical` / `warning` / `healthy` / `unknown` | int | 风险分层计数，四者之和 = `device_total` |
| `avg_battery` | float? | 窗口内最新电量的简单平均；无数据为 `null` |
| `warning_percent` / `critical_percent` | int | 分层阈值随响应返回（当前 30 / 15），前端不硬编码 |
| `window_days` / `window_start` | int / datetime | 统计窗口（UTC 自然日，含当天）与起点 |
| `trend` | `[{day_start, avg_battery, samples}]` | 每个有数据的 UTC 自然日的机队平均电量；`samples` 为参与计算的设备数 |
| `groups` | `[{group_name, total, critical, warning, healthy, unknown, avg_battery}]` | 按设备分组汇总，`group_name` 升序 |
| `watchlist` | `[{device_id, name, group_name, status, battery, voltage, last_report_at, drain_per_day, days_to_critical, risk, open_alert_count}]` | 仅含 `critical` / `warning`，排序：风险等级 → 预计天数升序 → 电量升序 → `device_id`；最多 `limit` 台 |
| `watchlist_total` | int | 风险设备总数（不受 `limit` 截断影响） |

口径与判定：

- **数据来源只有 `sensor_record`**：最新电量/电压/上报时间取窗口内该设备最后一条记录；
  窗口内没有任何记录的设备记为 `unknown`，**不插值、不补假值**；
- **衰减速度**：对设备每日平均电量按天做最小二乘拟合，输出 `drain_per_day`（百分点/天，正值为掉电）；
  日斜率小于 0.01 或电量回升时输出 `null`（稳定/在充电）；
- **预计到严重阈值**：`days_to_critical = (最新电量 - 15) / drain_per_day`，电量已 ≤15% 时为 `0`；
  无法估算时为 `null`；
- **风险分层**（任一条件命中即升级）：
  `critical` = 电量 ≤ 15% 或预计 ≤ 7 天；`warning` = 电量 ≤ 30% 或预计 ≤ 30 天；其余为 `healthy`；
- 分组筛选按 `device.group_name` 精确匹配，分组变更后下一次请求立即按新分组统计；
- **只读端点，不写审计、不推进任何状态机**；viewer / operator / admin 均可访问，
  参数越界返回 422。

**请求追踪（v1.20.0）**：所有 HTTP 响应都带 `X-Request-Id`；若请求方传入合法的
`X-Request-Id`（`[A-Za-z0-9._-]`，≤64 字符）则沿用，否则生成 32 位十六进制 ID。
错误响应体在原有 `{code, message, data}` 基础上新增 `requestId`：

```json
{"code": 500, "message": "服务内部错误", "data": null, "requestId": "trace-abc-123"}
```

日志中同一 ID 可用于 `docker compose logs gateway | jq 'select(.requestId=="...")'` 对齐排查。
`/metrics` 指标与告警口径见 [observability.md](observability.md)。

### 3.9 账号与权限（仅 admin）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/users` | 账号列表（用户名/角色/启停/最近登录） |
| POST | `/api/users` | 新建账号，入参 `{username, password, role}`；用户名重复返回 409 |
| PATCH | `/api/users/{username}` | 修改角色 / 启停 / 重置口令（`role`、`active`、`password` 均可选） |
| DELETE | `/api/users/{username}` | 删除账号；禁止删除当前登录账号 |

保护规则：系统必须保留至少一个可用管理员，降级/停用/删除最后一个 admin 均返回 400。
账号变更全部写入审计日志（`user.create` / `user.update` / `user.delete`），
审计 detail 只记录变更项，不落口令明文。

### 3.10 移动看板复用的网关接口（v1.7.0 只读 / v1.9.0 写操作 / v1.11.0 服务端分页，兼容增量）

device_app 的移动看板只消费既有网关端点，v1.11.0 仅使用 3.2 中新增的可选参数，未新增端点：

| 用途 | 接口 | 参数 / 说明 |
| --- | --- | --- |
| 登录 | `POST /api/auth/login` | `{username, password}` → `data: {token, refresh_token, token_type, expires_in, user}` |
| 设备列表 | `GET /api/devices` | `page` / `pageSize=20` / `status` / `keyword` / `group` / `tag`（v1.35.0，可重复） / `hasAlert` / `withAlertCount=true` |
| 分组概览 | `GET /api/devices/groups` | 分组 chips 数据源；空分组与「未分组」合并 |
| 标签列表 | `GET /api/devices/tags` | v1.35.0 标签 chips 数据源，含 `device_count` |
| 每设备最新值 | `GET /api/records/latest` | `deviceIds=<当前已加载页设备>`，只取已加载设备，避免全量 |
| 单设备历史上报 | `GET /api/devices/{id}/records` | 详情页分页：`limit=11&offset=<已取条数>`，多取 1 条用于判断是否还有更早数据，展示 10 条/页（v1.13.0） |
| 设备影子 | `GET /api/devices/{id}/shadow` | v1.36.0 详情只读展示期望、上报、版本与服务端差异；编辑仍在 Web |
| 设备影子历史 | `GET /api/devices/{id}/shadow/history` | v1.37.0 展开时按需加载，`limit=20&offset=<已取条数>`；只读完整快照，不提供移动端回滚 |
| 未处理告警 | `GET /api/alerts` | `status=open&page=1&pageSize=20`，`total` 为全量未处理数；v1.35.0 起可叠加 `tag`（可重复，或语义） |
| 指令回执 | `GET /api/devices/{id}/commands` | `limit=20` |
| 汇总卡 | `GET /api/stats/overview` | `device_total` / `device_online` / `alert_open` 等服务端全量统计 |

统一鉴权头 `Authorization: Bearer <token>`；token 由登录接口下发，与 Web 后台一致。
v1.24.0 起登录同时返回 `refresh_token`：看板对 `401` 的处理改为「本页单飞 refresh 一次 → 重放原请求」，
refresh 失败才清除 access/refresh 并回登录页；**并发请求共享同一次刷新**（`SessionRefreshCoordinator`），
避免多个 401 触发刷新风暴。access 与 refresh 仍保存在同一个 Keychain/Keystore key（`mobile.session`）：
两者同属一份会话、生命周期一致，分 key 会让「清一半」的异常状态更易出现。
`403` 提示无权访问；超时与网络错误展示可读文案。v1.9.0 起额外调用 3.10.2 列出的两个写接口，
写权限与 403 处理不复制 RBAC，直接沿用登录会话角色与网关响应。

#### 3.10.1 移动看板实时订阅（v1.8.0）

v1.8.0 起看板增量数据改走 MQTT over WebSocket（默认 `ws://<网关主机>:9001`），
**不新增 Topic、不修改报文格式**，仅订阅既有 Topic：

| 用途 | Topic | 说明 |
| --- | --- | --- |
| 新告警 | `device/system/alert` | 报文见 2.5（camelCase），按 `alertId` 去重后插入头条 |
| 设备上报 | `device/+/sensor/report` | 报文见 2.1，按 `deviceId` 覆盖最新值并标记在线 |
| 设备上下线 | `device/+/status` | 报文见 2.2，优先级高于 REST 快照，LWT 遗嘱离线同样生效 |
| 设备分组变更 | `device/system/group` | 报文见 2.6（v1.14.0）；就地更新已加载设备的分组名 |

- 9001 为 Mosquitto 的 WebSocket 监听端口；移动端与浏览器一样无法直连 TCP 1883；
- 实时地址可在看板设置中单独配置，留空则按网关主机推导为 `ws://<网关主机>:9001`；
- 断线由客户端按 3s → 30s 指数退避自动重连，期间降级为 REST 轮询（15 秒）并提示「已切换轮询」，
  恢复连接后停止轮询；首屏与手动刷新仍走 3.10 的 REST 接口；
- 看板切到后台或切换到设备模拟页时断开实时连接与轮询，回到看板后自动恢复；
- 移动看板的实时层只订阅、不发布；写操作走 REST 落库后由网关发布到 `cmd` Topic，
  因此不影响设备模拟器的 1883 链路；
- 实时链路失败只影响界面状态条，不阻塞 REST 数据展示。

#### 3.10.2 移动看板写操作（v1.9.0）

写操作复用 3.3 / 3.7 的既有端点，角色要求与 Web 完全一致（operator + admin，viewer 返回 403）：

| 操作 | 接口 | 入参 | 返回 | 角色 |
| --- | --- | --- | --- | --- |
| 指令下发 | `POST /api/devices/{deviceId}/commands` | `{type, payload}`；type ∈ `calibrate` / `ota` / `reboot` / `config` | `data: CommandOut`（`cmd_id` / `state` / `progress` / `message`） | operator / admin |
| 告警确认 | `POST /api/alerts/{alertId}/ack` | 可选 `{note}`（≤500 字） | `data: AlertOut`（`status=acked`，含 `acked_by` / `acked_at` / `ack_note`） | operator / admin |
| 批量确认 | `POST /api/alerts/bulk-ack` | `{alert_ids, note?}` | `data: BulkAckResult`，逐类展示部分失败 | operator / admin |

- 网关只校验 `type` 枚举与设备 / 告警是否存在，`payload` 的键值由客户端先行校验：
  calibrate `{temperatureOffset, humidityOffset}`、config `{reportIntervalSec, sleepSec}`、
  reboot `{}`、ota `{targetVersion, url?, size?}`；
- 错误语义沿用统一 envelope：401 未登录、403 无权限、404 设备 / 告警不存在、
  409 冲突（按服务端 `detail` 展示）、503 MQTT 下发失败；
- 客户端不复制 RBAC，仅根据登录会话角色与 403 响应禁用写按钮；
- 提交中禁用按钮防重复点击；告警确认成功即从本地「未处理」列表移除；
  指令下发后展示 `CommandOut` 回执，并在设备详情刷新 `GET /api/devices/{id}/commands`。

移动看板的 Widget 测试通过构造参数注入 fake `ApiClient`、实时服务与存储，
覆盖上述交互并确保测试过程不产生真实写请求；契约断言仍以本节的端点与错误语义为准。

#### 3.10.3 移动看板服务端分页与实时协同（v1.11.0）

v1.11.0 拆掉「首屏 100 台」与「告警 pageSize=50」两个客户端上限：

| 行为 | 契约 |
| --- | --- |
| 设备首屏 | `GET /api/devices?page=1&pageSize=20&withAlertCount=true`，按 `page * pageSize < total` 判断是否还有下一页 |
| 滚动加载 | 设备区页脚进入视口即请求下一页并原样追加；加载中展示「加载中…」，`已加载条数 == total` 时展示「没有更多了」 |
| 搜索/状态筛选 | `keyword` / `status` 作为请求参数下发，300ms 防抖后重置 `page=1`，避免把旧页拼进新筛选 |
| 标签筛选 | v1.35.0：`tag` 与 `keyword` / `status` / `group` / `hasAlert` 同时下发（「且」组合），切换标签立即重置 `page=1`；不选标签时不带该参数，行为与升级前一致 |
| 有告警筛选 | `hasAlert=true`（数据库层聚合 `status='open'`）+ `withAlertCount=true`；卡片角标显示 `open_alert_count`，与筛选结果同源，不再依赖告警列表前 50 条 |
| 告警分页 | `GET /api/alerts?status=open&page=N&pageSize=20`；标题「未处理告警（N）」使用服务端 `total`，列表按页追加 |
| 汇总卡 | `GET /api/stats/overview` 的 `device_total` / `device_online` / `alert_open`，与分页口径解耦，不会因只看第 1 页而失真 |
| 实时上报 | 只更新已加载设备的数值/在线状态，不触发整页重拉；新设备或不满足当前页的设备**不会即时插入**，在下次刷新（下拉、筛选切换、轮询兜底）时按服务端分页结果出现 |
| 实时告警 | 插入列表头条并递增服务端计数与对应已加载设备角标；未加载设备的下次刷新补齐 |
| 上下线事件 | 立即改变已加载设备在「在线 / 离线」筛选中的可见性；分页游标与 `total` 仍以服务端为准 |

上述行为均有 Widget 测试覆盖（翻页加载、搜索重置分页、聚合告警筛选、末页/空状态）。

#### 3.10.4 设备详情历史上报分页（v1.13.0）

`GET /api/devices/{device_id}/records` 新增可选 `offset`（默认 `0`，`ge=0`），
不新增端点、不改变响应结构（仍为 `data: [SensorRecordOut]`）：

| 参数 | 语义 |
| --- | --- |
| `limit` | 本页最多返回条数，范围 1~2000（默认 500，与历史一致） |
| `offset` | 从**最新一条**开始跳过的条数；按 `ts desc` 跳过 `offset` 后取 `limit` 条，最终仍翻转为时间正序返回 |
| 不传 `offset` | 生成 SQL 时不带 OFFSET 子句，行为与旧版完全一致（最近 `limit` 条、时间正序） |

客户端（device_app 详情弹层）：

- 每页展示 10 条，请求 `limit=11&offset=<已从服务端取回条数>`；多取的 1 条只用于判断
  `hasMore`（返回条数 > 10），不进入列表；
- 首屏成功后按时间倒序展示（最新在前）；「加载更早」或滚动到底按 `offset` 追加更早一页；
- 实时新上报插入列表顶部；用户已向下浏览时按插入行实际高度补偿滚动偏移，停留在顶部时直接展示新数据；
- 三态文案：加载中「加载中…」/「正在加载更早数据…」，到底「没有更早数据」，首页即空「暂无历史上报」。

网关侧回归测试：`gateway/tests/test_device_service.py` 覆盖
「不传 offset 与显式 offset=0 结果一致」与「offset 逐页向更早取数、越界返回空列表」。

#### 3.10.5 设备分组变更实时化（v1.14.0）

| 行为 | 契约 |
| --- | --- |
| 事件来源 | 后台 `PATCH /api/devices/{deviceId}` 改分组，网关提交后发布 `device/system/group`（报文见 2.6） |
| 已加载设备 | 收到事件立即把该设备条目的 `group_name` 就地更新，不等待轮询 |
| 分组筛选 | 改组后已不属于当前筛选分组时**立即移出列表**，`total` 同步减一；随后按服务端分页结果校正 |
| 分组 chips | 事件后防抖 250ms 重拉 `GET /api/devices/groups`，chips 始终等于 REST 快照；不做本地增减（并发改名或整组清空时本地增量会与服务端不一致） |
| 分页与汇总 | 对账刷新同时更新 `total` 与 `GET /api/stats/overview`；已加载多页时回到第 1 页重取（与轮询兜底一致），避免「列表缺一条但 total 未变」 |
| 轮询降级 | 实时断开期间事件收不到，但下次轮询（15 秒）或手动刷新会用 REST 结果覆盖分组与 chips，不存在「永远看不到」 |
| 未加载设备 | 改组后新进入当前筛选的设备不即时插入，由对账刷新/下次分页按服务端结果出现（与 3.10.3 的实时上报策略一致） |

回归覆盖：`gateway/tests/test_device_group_event.py`（事件字段、设备不存在、分组未变化、发布失败降级）、
`device_app/test/realtime_test.dart`（报文解析与脏报文）、
`device_app/test/mobile_board_widget_test.dart`（移出当前筛选、chips 刷新）、
`scripts/e2e/e2e_acceptance.py` 的 `group_change`（真实 Broker + WebSocket + REST 口径一致）。

#### 3.10.6 告警中心（v1.15.0）

| 行为 | 契约 |
| --- | --- |
| 列表 | `GET /api/alerts?deviceId&level&status&page&pageSize`，三组筛选可组合；切换筛选重置 `page=1`；`total` 为过滤后全量 |
| 分页 | `pageSize=20`，滚动到页脚请求 `page+1` 并追加；加载中 / `page * pageSize >= total`（没有更多）/ 首页空态三态可读 |
| 每行 | 设备（名称优先，取 `GET /api/devices?pageSize=200` 的本地映射，缺失时回落 `device_id`）、`metric`、`value`、`threshold`、`level`（warning/critical）、`created_at`、`status`；已确认行展示 `acked_by` / `acked_at` / `ack_note`，历史行显示「历史确认（无处置记录）」 |
| 批量确认 | 仅 open 行可勾选；`POST /api/alerts/bulk-ack` 一次提交（可选 `note` ≤500 字），成功后按 `acked` 移除行、`total` 与未读角标同步减少；`already_acked` / `not_found` 在页面提示，不当作成功数量 |
| 实时 | `device/system/alert`（报文见 2.5）按 `alertId` 去重；满足当前 level/status/device 筛选时插入列表顶部，否则只累加角标；实时断开时 15 秒轮询第 1 页 + `summary` 补齐 |
| 角标口径 | 本地未读计数：初始 = `GET /api/alerts/summary.open`；实时新增 +1；进入告警中心清零；页面不可见时的轮询以服务端 `open` 总数取大值补齐，页面可见时保持清零口径 |
| 权限 | 与 3.10.2 一致：viewer 可勾选但批量确认按钮禁用并提示；operator/admin 可提交；403 提示后禁用本轮写操作；401 清会话回登录页 |
| 兼容 | `GET /api/alerts` 不传新参数时行为不变；v1.21.0 起级别收窄为 warning/critical，`info`/`notice` 返回 422（规则引擎从未产出这两级，无历史行受影响） |

回归覆盖：`gateway/tests/test_alert_service.py`（筛选 / 分页 / 批量部分失败 / 全失败）、
`gateway/tests/test_http_regression.py`（批量确认 HTTP 权限与部分失败、备注与处置元数据、级别收窄 422、PATCH 设备在 MQTT 不可用时仍 200）、
`device_app/test/alert_center_widget_test.dart`（筛选切换、分页、空态、批量成功、备注输入与处置信息展示、viewer 禁用、403、实时入顶与角标清零）、
`scripts/e2e/e2e_acceptance.py` 的 `alert_center_bulk_ack`（真实 Broker 产生告警 → REST 筛选可见 → 批量确认后 open 不再返回）。

#### 3.10.7 报表与数据导出（v1.30.0）

服务端直接生成 CSV，前端不做拼接、不翻页拉全量。两个端点共用同一套筛选语义
（设备历史复用 3.2 / 3.10.4 的 `deviceId` + `start`/`end`，告警复用 3.3 / 3.10.6 的
`level`/`status`，两者都支持按分组 `group` 过滤）：

| 方法 | 路径 | Query |
| --- | --- | --- |
| GET | `/api/exports/records` | `deviceId`、`group`、`tag`（v1.35.0，可重复，或语义）、`start`、`end` |
| GET | `/api/exports/alerts` | `deviceId`、`group`、`tag`（v1.35.0，可重复，或语义）、`level`(warning/critical)、`status`(open/acked)、`start`、`end` |

- `start` / `end` 为闭区间，接受 ISO8601 或毫秒时间戳；结束早于开始返回 400。
- 告警级别沿用 v1.21.0 收窄后的 `warning|critical`，传其他值由 FastAPI 返回 422。
- `group` 精确匹配 `Device.group_name`；设备不存在或未分组时该行不会因分组筛选被返回。
- `tag` 可重复（`?tag=冷库&tag=关键`），多个标签为「或」：设备持有任一标签即导出该设备的记录/告警；
  与 `deviceId` / `group` / `level` / `status` / 时间范围同时存在时为「且」。不传 `tag` 时导出范围与升级前一致。
- 未传时间范围时导出全部可用数据，文件名用 `all` 标记（不会伪造一个看起来像区间的范围）。

**流式与上限**

- 采用 `StreamingResponse` + keyset 分页（记录按 `(ts,id)`、告警按 `(created_at,id)`），
  每批 2000 行写入一个缓冲区后立即发送并清空，内存不随总行数增长。
- 单次导出上限 `EXPORT_MAX_ROWS`（默认 200,000）。先 `count` 再开始流式，超限返回
  **422**：`导出 N 行超过上限 200000 行，请缩小时间范围，或按设备 / 分组分批导出`。
  选择报错而不是静默截断：周报/对账场景里少几行比导出失败更难发现。
- 上限只约束单次请求；月报可用「按月 + 按分组」拆分多次导出后合并。

**CSV 格式**

- UTF-8 + BOM（`EF BB BF`），Excel 双击直接识别中文；行结束符 `CRLF`。
- 中文表头；时间列统一为 `YYYY-MM-DD HH:MM:SS`（UTC），表头注明 `(UTC)`。
- 逗号 / 双引号 / 换行按 RFC 4180 转义；纯数字前导零（如设备 ID `001`）写成
  `="001"` 文本公式，避免 Excel 吃掉前导零。
- 以 `= + - @`、Tab、回车开头的文本加 `'` 前缀，防止被 Excel 当作公式执行。
- 敏感字段（MQTT 口令、凭证指纹、用户口令哈希等）**不在任何导出列中**。

**响应头**

| 头 | 值 |
| --- | --- |
| `Content-Type` | `text/csv; charset=utf-8` |
| `Content-Disposition` | `attachment; filename="<kind>_<start>_<end>.csv"`；`kind` 为 `records` / `alerts`，日期取 UTC 的 `YYYY-MM-DD`；无时间范围时为 `<kind>_all_<今天>.csv` |
| `Cache-Control` | `no-store`（导出含业务数据，不允许中间层缓存） |

**权限与审计**

- 仅 `operator` / `admin` 可导出；`viewer` 返回 403。批量导出把大量业务数据带出系统，
  外泄面远大于单页查询，因此要求可追责账号。
- 导出完成后写审计：`action=export.records` / `export.alerts`，
  `target_type=export`、`target_id=文件名`，`detail={filters, rows, duration_ms, filename}`。
  审计只记录筛选范围、实际行数、耗时与文件名，**不记录任何导出内容**；客户端中途断开
  （流被取消）也会在生成器 `finally` 中落一条审计。
- `GET /api/alerts` 同步新增可选 `start` / `end`（`created_at` 闭区间），保证列表看到的
  范围与导出范围由同一段 SQL 语义产生。

回归覆盖：`gateway/tests/test_exports.py`（筛选复用、CSV 转义/BOM/前导零/UTC、空结果、
权限矩阵、超限 422、审计内容）；`web/src/api/exports.spec.ts`（参数传递、文件名解析、
错误提示、401 刷新失败）；真实大数据验证见 `scripts/verify_exports.py`
（20 万行 Postgres 实测，不进常规 CI）。

---

## 4. 数据库表结构

### 4.1 `device` 设备表

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| device_id | varchar(64) PK | 设备唯一 ID |
| name | varchar(128) | 设备名称 |
| model | varchar(64) | 型号 |
| firmware_version | varchar(32) | 当前固件版本 |
| group_name | varchar(64) | 设备分组，默认「默认分组」 |
| status | varchar(16) | online / offline |
| last_report_at | timestamptz | 最后上报时间 |
| last_online_at | timestamptz | 最后上线时间 |
| mqtt_username | varchar(64) | MQTT 用户名，恒等于 device_id（v1.17.0） |
| mqtt_credential_version | int | 凭证版本，每次签发/轮换 +1（v1.17.0） |
| mqtt_credential_fingerprint | varchar(64) | 口令 SHA-256 指纹，用于对账；**不存明文**（v1.17.0） |
| mqtt_credential_issued_at | timestamptz | 首次签发时间（v1.17.0） |
| mqtt_credential_rotated_at | timestamptz | 最近轮换时间（v1.17.0） |
| created_at / updated_at | timestamptz | 时间戳 |

### 4.2 `sensor_record` 传感器时序表

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigserial PK | 自增 |
| device_id | varchar(64) | 关联设备，索引 |
| temperature / humidity / voltage | numeric(10,2) | 传感器读数 |
| rssi | int | 信号强度 |
| battery | int | 电量 |
| seq | bigint | 设备序号 |
| ts | timestamptz | 采集时间，索引 `(device_id, ts desc)` |

### 4.3 `device_alert` AI 告警表

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigserial PK | 自增 |
| alert_id | varchar(64) UNIQUE | 业务告警 ID |
| device_id | varchar(64) | 关联设备 |
| level | varchar(16) | warning / critical |
| metric | varchar(32) | temperature / voltage / humidity / rssi |
| value / threshold | numeric(10,2) | 触发值 / 阈值 |
| message | text | AI 生成的告警文案 |
| ai_generated | boolean | 是否由 LLM 生成 |
| status | varchar(16) | open / acked |
| created_at | timestamptz | 触发时间 |
| ack_note | text NULL | 处置备注（v1.21.0，≤500 字由 API 校验） |
| acked_by | varchar(64) NULL | 确认人；v1.21.0 之前的历史确认行为 NULL |
| acked_at | timestamptz NULL | 确认时间；历史确认行为 NULL |
| assignee | varchar(64) NULL | 责任人（v1.33.0）；未指派为 NULL |
| sla_minutes | int NOT NULL | 创建时的 SLA 时限快照（v1.33.0，分钟），此后改配置不影响既有告警 |
| sla_deadline_at | timestamptz NOT NULL | 创建时计算的截止时间 = `created_at + sla_minutes`（v1.33.0） |
| escalation_count | int NOT NULL DEFAULT 0 | 已发出的升级次数（v1.33.0），满上限即停止 |
| last_escalated_at | timestamptz NULL | 最近一次升级时间（v1.33.0） |
| matched_rule | jsonb NULL | 命中规则快照（v1.34.0）：`rule_id/rule_name/op/threshold/level/value/consecutive`；历史行为 NULL |

索引 `idx_alert_status_sla_deadline(status, sla_deadline_at)` 支撑巡检与「已超时」筛选。

### `sla_config`（v1.33.0，单行）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | int PK | 固定为 1 |
| critical_minutes | int | critical 告警响应时限，默认 30，1~1440 |
| warning_minutes | int | warning 告警响应时限，默认 120，1~1440 |
| max_escalations | int | 单条告警最多升级次数，默认 2，0~10（0 = 不升级） |
| escalation_interval_minutes | int | 相邻升级间隔，默认 30，1~1440 |
| updated_at / updated_by | timestamptz / varchar(64) | 最近修改时间与人员 |

### 4.4 支撑表（OTA / 指令 / 设备影子链路必需）

### `device_shadow`（v1.36.0）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| device_id | varchar(64) PK | 一行一设备；删除设备时由设备服务显式清理 |
| desired / reported | jsonb（SQLite 为 JSON） | 云端期望 / 设备实际上报的扁平键值对象 |
| version | int | 期望版本；只有实际变更才 +1 |
| reported_version | int | 设备已应用版本；只前进并夹紧到 version |
| desired_updated_at | timestamptz NULL | 最近实际期望变更时间 |
| desired_updated_by | varchar(64) NULL | 最近期望修改人 |
| reported_updated_at | timestamptz NULL | 最近上报时间 |
| created_at / updated_at | timestamptz | 行创建与更新时间 |

同步状态和差异不落库，由 `desired`、`reported` 与设备当前在线状态实时派生。

### `device_shadow_revision`（v1.37.0）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigserial PK | 快照 ID |
| device_id | varchar(64) | 设备 ID；删除设备时显式清理历史 |
| version | int | 期望版本；与 device_id 组成唯一约束 `uq_shadow_revision_device_version` |
| desired | jsonb（SQLite 为 JSON） | 该版本变更后的完整、不可变期望快照 |
| changed_keys | jsonb（SQLite 为 JSON） | 相对前一状态新增、删除或值变化的键，排序后保存 |
| action | varchar(16) | baseline / merge / clear / batch / rollback |
| source_version | int NULL | rollback 的目标快照版本，其余动作为 NULL |
| created_at | timestamptz | 快照创建时间 |
| created_by | varchar(64) NULL | 操作人；空影子 baseline 可为 NULL |

历史只记录 `desired`，不复制 reported。升级时仅从 `device_shadow` 回填当前版本基线；
downgrade 只删除本表，不删除或回退 `device_shadow` 当前状态。

### `device_command`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| cmd_id | varchar(64) PK | 指令 ID |
| device_id | varchar(64) | 目标设备 |
| type | varchar(16) | calibrate / ota / reboot / config |
| payload | jsonb | 指令参数 |
| state | varchar(16) | issued / received / progress / success / failed（后两者为不可回退终态） |
| progress | int | 0 ~ 100；success 固定 100 |
| message | varchar(255) | 回执描述；超时巡检写入「设备超时未回执（超过 N 秒）」 |
| ota_task_id | varchar(64) NULL | 归属 OTA 任务 |
| operator | varchar(64) | 下发人 |
| created_at / updated_at | timestamptz | 时间戳；`updated_at` 是超时巡检的判据（每次有效回执刷新） |

### `ota_task`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| task_id | varchar(64) PK | 任务 ID |
| target_version | varchar(32) | 目标版本 |
| url | varchar(512) | 固件地址 |
| description | varchar(255) | 任务说明 |
| total / success / failed | int | 汇总统计 |
| created_at | timestamptz | 创建时间 |

> 原需求「三张表足够」指核心业务三张表；OTA 管理页需要持久化任务与回执状态，
> 因此额外增加 `device_command`、`ota_task` 两张支撑表，属于同一业务域，不引入额外技术栈。

### `rule_config` 阈值在线配置表（v1.34.0 起为兼容层）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| key | varchar(64) PK | 配置键（与 Settings.threshold_* 属性同名） |
| value | float | 阈值数值 |
| updated_by | varchar(64) | 最后修改人 |
| updated_at | timestamptz | 最后修改时间 |

v1.34.0 起判定读 `alert_rule` 表；本表由旧 `/api/rules/thresholds` 写入时同步维护，
并作为 downgrade 回 v1.33.0 后的阈值来源。启动时仍全量加载进进程缓存，供旧接口视图使用。

### `alert_rule` 告警规则表（v1.34.0）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigserial PK | 自增 |
| name | varchar(64) | 规则名称（告警详情展示） |
| metric | varchar(32) | voltage / temperature / humidity / rssi |
| op | varchar(8) | lt（低于触发）/ gt（高于触发） |
| threshold | numeric(10,2) | 阈值 |
| level | varchar(16) | warning / critical |
| scope_type | varchar(16) | global / group / device / tag / custom（v1.38.0） |
| scope_value | varchar(64) NULL | 分组名或设备 ID；global 为 NULL |
| scope_values | jsonb NULL | v1.35.0 多值范围；条件树规则为镜像值 |
| conditions | jsonb NULL | v1.38.0 条件树（all/any 嵌套）；NULL 表示走老字段语义 |
| enabled | boolean | 停用后不参与判定 |
| consecutive_count | int DEFAULT 1 | 连续 N 次越限才触发（1~20） |
| priority | int DEFAULT 100 | 同指标同级别裁决顺序（小者优先） |
| is_default | boolean DEFAULT false | v1.33.0 默认阈值种子（共 10 条） |
| created_at / updated_at | timestamptz | 时间戳，`updated_at` 供「最近修改时间」展示 |
| updated_by | varchar(64) | 最近修改人 |

索引：`idx_alert_rule_metric(metric)`、`idx_alert_rule_scope(scope_type, scope_value)`。

默认规则由迁移 `0009_alert_rules` 种子（读取 `rule_config` 覆盖值，否则 v1.33.0 默认阈值），
空表时网关启动也会幂等补齐。downgrade 会删除本表与 `device_alert.matched_rule`。

### `audit_log` 审计日志表

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigserial PK | 自增 |
| operator | varchar(64) | 操作人（登录用户名） |
| action | varchar(32) | 动作：login / command.create / ota.create / alert.ack / threshold.update / alert_rule.* / notify.config_update / user.* / device.* |
| target_type | varchar(32) | 目标类型（device / alert / ota_task / rule_config / alert_rule / app_user） |
| target_id | varchar(64) | 目标标识 |
| detail | jsonb | 动作上下文（如指令类型、阈值变更明细；告警规则只记字段名，不记阈值） |
| created_at | timestamptz | 操作时间，索引 |

动作枚举：`login` / `command.create` / `command.bulk_create` / `ota.create` / `alert.ack` /
`threshold.update` / `notify.config_update` / `user.create` / `user.update` / `user.delete` /
`alert_rule.create` / `alert_rule.update` / `alert_rule.enable` / `alert_rule.disable` /
`alert_rule.delete` / `alert_rule.restore_defaults`（v1.34.0） /
`device.shadow_desired_update` / `device.shadow_desired_batch`（v1.36.0） /
`device.shadow_rollback`（v1.37.0）。

### `app_user` 后台账号表

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| username | varchar(64) PK | 登录用户名 |
| password_hash | varchar(255) | PBKDF2-SHA256 哈希（`pbkdf2_sha256$迭代$salt$hash`） |
| role | varchar(16) | admin / operator / viewer |
| active | boolean | 是否启用；停用后立即无法登录 |
| last_login_at | timestamptz | 最近登录时间 |
| created_at / updated_at | timestamptz | 时间戳 |

首次启动且表为空时，网关按环境变量 `ADMIN_USERNAME/ADMIN_PASSWORD` 初始化管理员；
演示账号 `operator/operator123` 与 `viewer/viewer123` 仅在 `SEED_DEMO_USERS` 生效时写入
（开发/测试默认开启，生产默认关闭，显式设为 `true` 才写入并打印 warning）。
已有账号时不再覆盖，避免重启重置运维改过的口令。`APP_ENV=production` 且仍使用默认
JWT/管理员口令/SQLite 时，网关启动直接失败。

### 4.5 告警通知表（v1.29.0）

#### `notification_config`

单行配置表（`id=1`），每次告警派发热读取，因此界面保存后无需重启。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | int PK | 固定 1 |
| enabled | boolean | 总开关 |
| webhook_enabled / webhook_url / webhook_secret | bool / varchar(512) / varchar(128) | Webhook 通道与 HMAC 密钥 |
| webhook_timeout_seconds / webhook_max_attempts | int | 超时与最大尝试 |
| smtp_enabled / smtp_host / smtp_port | bool / varchar(128) / int | SMTP 通道 |
| smtp_username / smtp_password | varchar(128) | 登录凭据；接口不回显 |
| smtp_from / smtp_to | varchar(255) | 发件人 / 收件人（逗号分隔） |
| smtp_starttls | boolean | STARTTLS |
| smtp_timeout_seconds / smtp_max_attempts | int | 超时与最大尝试 |
| levels / groups / device_allow / device_deny | varchar(255) | 逗号分隔的规则集合；空 = 不限制 |
| mute_minutes | int | 静默窗口，0 = 不去重 |
| retry_backoff_seconds | numeric(10,2) | 指数退避基数 |
| updated_at / updated_by | timestamptz / varchar(64) | 最近修改与操作人 |

#### `notification_delivery`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigserial PK | 自增；同时作为投递去重键下发接收端 |
| alert_id | varchar(64) | 关联告警 ID（测试通知为 `test-*`） |
| device_id / metric / level | varchar | 规则与排障维度 |
| rule_id / rule_name / group_name | int / varchar | 命中的规则与告警时设备分组（v1.31.0） |
| channel | varchar(16) | `webhook` / `smtp` / `mute`（静默）/ `rule`（未匹配规则，v1.31.0） |
| status | varchar(16) | `pending`（待续投，v1.31.0）/ `sent` / `failed` / `suppressed` |
| attempts | int | 实际尝试次数 |
| error | varchar(255) | 脱敏后的失败原因；成功为 NULL |
| duration_ms | int | 各次尝试累计耗时（v1.31.0） |
| next_attempt_at | timestamptz | 下次重试时间；终态或 `sent` 为 NULL（v1.31.0） |
| finished_at | timestamptz | 进入终态的时间（v1.31.0） |
| created_at / updated_at | timestamptz | 创建与最近一次尝试时间；索引 `(created_at)`、`(device_id, metric, created_at)`、`(status, next_attempt_at)`、`(channel, created_at)` |

#### `notification_rule`（v1.31.0）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | serial PK | 自增 |
| name | varchar(64) | 规则名（界面展示、投递记录冗余） |
| enabled | boolean | 是否参与裁决 |
| priority | int | 越小越优先；同值按 `id ASC` |
| levels / groups / device_allow / device_deny | varchar(255) | 同 `notification_config` 的逗号分隔语义；空 = 不限制 |
| webhook_enabled / webhook_url / webhook_secret | bool / varchar(512) / varchar(128) | 规则级 Webhook 接收人 |
| smtp_enabled / smtp_to | bool / varchar(255) | 规则级收件人；服务器/口令沿用 `notification_config` |
| created_at / updated_at / updated_by | timestamptz / timestamptz / varchar(64) | 时间戳与最后操作人 |

迁移 `0007` 从既有 `notification_config` 回填一条 `priority=100` 的默认规则，
保证 v1.29.0 升级后行为不变；`downgrade` 删除规则表与 v1.31.0 新增列，不删除既有配置与投递行。

---

## 5. 阈值与 AI 诊断规则

| 指标 | warning | critical |
| --- | --- | --- |
| voltage | < 3.4V | < 3.2V |
| temperature | > 60℃ 或 < -10℃ | > 75℃ 或 < -20℃ |
| humidity | > 90% | > 97% |
| rssi | < -100dBm | < -110dBm |

- 网关对每条上报执行规则判定，命中即生成告警。
- 告警文案优先调用 LLM（OpenAI 兼容接口）生成；未配置 `LLM_API_KEY` 时走内置规则模板，
  保证离线环境可完整演示链路。
- 同一设备同一指标 5 分钟内只告警一次（抑制抖动）。
- 上表为环境变量默认值；运维侧可通过 `PUT /api/rules/thresholds` 在线调整，
  覆盖值持久化于 `rule_config` 表并即时生效，重启后保留。
