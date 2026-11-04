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
  "ts": 1762137600000
}
```

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
| GET | `/api/devices` | 列表，Query：`status`(online/offline)、`keyword`、`group`、`hasAlert`、`withAlertCount`、`page`、`pageSize` |
| GET | `/api/devices/groups` | 分组概览：每组 `total` / `online` / `offline` |
| POST | `/api/devices` | 手工注册设备，入参 `device_id`（3~64 位字母数字 _-）、`name`、`model`、`firmware_version`、`group_name`；重复 ID 返回 409 |
| POST | `/api/devices/bulk` | 批量注册，单次最多 200 台，返回 `{created, skipped}`，重复 ID 跳过 |
| POST | `/api/devices/{deviceId}/mqtt-credential` | 签发/轮换设备 MQTT 凭证（operator+admin，v1.17.0） |
| PATCH | `/api/devices/{deviceId}` | 编辑名称 / 型号 / 固件版本 / 分组；设备 ID 与状态不可改 |
| DELETE | `/api/devices/{deviceId}` | 删除设备（仅 admin）；存在历史时序数据时返回 409 |
| GET | `/api/devices/{deviceId}` | 详情 + 最新一条传感器数据 |
| GET | `/api/devices/{deviceId}/records` | 历史数据，Query：`start`、`end`（ISO8601 或毫秒时间戳）、`limit` |
| GET | `/api/devices/{deviceId}/records/aggregate` | 时序聚合，Query：`bucket`(hour/day)、`start`、`end`，返回每桶 count 与三指标 avg/min/max |
| GET | `/api/records/latest` | 全局最新数据（仪表盘首屏兜底），Query：`status`(online/offline)、`keyword`、`limit`(1~500)、`offset`、`deviceIds`，均可选 |

`/api/records/latest` 的过滤与分页参数全部可选：不传时语义与历史一致（每个设备各自最新一条、
按时间倒序、不截断）；`keyword` 同时匹配设备 ID 与名称，`status` 依赖设备表关联。
`deviceIds` 为逗号分隔的设备 ID（单次最多 200 个，自动去重），只返回这些设备的最新值；
传空串返回空数组，便于看板按已加载页分批取值，不再全量拉取。

`/api/devices` 的 `status` / `keyword` / `group` / `page` / `pageSize` 同样为可选参数，
不传时默认第一页 20 台、不加过滤。v1.11.0 新增两个可选参数，默认关闭、不影响既有调用方：

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| `hasAlert` | `true` / `false`（默认） | 仅返回存在未处理告警的设备；筛选在数据库层按 `device_alert.status='open'` 聚合，不依赖告警列表分页大小 |
| `withAlertCount` | `true` / `false`（默认） | 每个设备条目额外返回 `open_alert_count`（未处理告警数，无告警为 0）；不传时该字段为 `null`，既有字段语义不变 |

设备条目（`DeviceOut`）字段：`device_id`、`name`、`model`、`firmware_version`、`group_name`、
`status`、`last_report_at`、`last_online_at`、`open_alert_count`（可选，仅 `withAlertCount=true` 时填充）。

v1.12.0 起 `group` 参数支持「未分组」：传 `group=未分组` 时匹配 `group_name` 为空串或字面「未分组」的设备；
`GET /api/devices/groups` 也把空分组名与「未分组」合并为同一个 `未分组` 桶（`total` / `online` / `offline` 累加）。
不传 `group` 时行为不变；普通组名筛选语义不变。

写操作权限：注册 / 批量注册 / 编辑 / 签发设备凭证为 operator+admin，删除为 admin。

**设备 MQTT 凭证（v1.17.0）**：`POST /api/devices/{deviceId}/mqtt-credential`

- 用户名恒等于 `device_id`，凭证与 `deviceId` 强绑定；每次调用都会生成新口令并覆盖旧值，
  因此同一接口既是**首次签发**也是**轮换**。设备注册后、首次连接 Broker 前必须调用一次。
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
      "reloaded": true,
      "reload_detail": "已向 Mosquitto（pid=12345）发送 SIGHUP"
    }
  }
  ```
- 存储位置：Broker 的 `password_file` 只保存 PBKDF2-SHA512 hash；`device` 表只保存
  `mqtt_username`、`mqtt_credential_version`、SHA-256 `fingerprint`、签发/轮换时间，
  **不保存明文口令**。`DeviceOut` 返回上述元数据，不返回 fingerprint 与口令。
- `reloaded=false` 表示口令已写入但 Broker 未重载（此时旧口令仍有效），`reload_detail`
  给出原因；可手工执行 SIGHUP 后重试连接。
- 写密码文件失败返回 503；设备不存在返回 404；未登录 401、viewer 403。
- 轮换后旧凭证在 Broker 重载后立即失效；吊销使用
  `scripts/mqtt_credentials.py revoke --subject device:<deviceId> --reload`。

v1.14.0 起 `PATCH /api/devices/{deviceId}` 是 Web 后台改分组的唯一入口（后台没有批量改分组），
分组变化时网关额外发布 2.6 的 `device/system/group` 事件；接口入参、响应与错误语义不变，
事件仅在 MQTT 可用时产生，旧客户端不订阅该 Topic 即保持原有行为。

### 3.3 告警

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/alerts` | 列表，Query：`deviceId`、`level`(warning/critical)、`status`(open/acked)、`page`(≥1)、`pageSize`(1~200)；`total` 为过滤后总数 |
| POST | `/api/alerts/bulk-ack` | 批量确认（v1.15.0 新增，v1.21.0 支持备注），见 3.3.1 |
| POST | `/api/alerts/{alertId}/ack` | 单条告警确认（v1.21.0 支持备注），见 3.3.2 |
| GET | `/api/alerts/summary` | 告警统计：`{open, critical_open, today}` |

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

### 3.4 阈值在线配置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/rules/thresholds` | 当前生效阈值（分组视图），每项标注 `source`：override=在线覆盖 / env=环境变量 |
| PUT | `/api/rules/thresholds` | 在线更新，入参只传待改键（如 `{voltage_warning: 3.3}`），持久化到 `rule_config` 并即时生效，写入审计日志 |

可配置键共 10 项：`voltage_warning/critical`、`temperature_warning_high/critical_high/warning_low/critical_low`、
`humidity_warning/critical`、`rssi_warning/critical`，取值范围按物理意义校验（见网关 `ThresholdsUpdateRequest`）。

### 3.5 审计日志

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/audit-logs` | 分页查询，Query：`operator`、`action`、`page`、`pageSize`；动作枚举：`login` / `command.create` / `ota.create` / `alert.ack` / `threshold.update` |

### 3.6 告警通知（Webhook）

网关配置 `ALERT_WEBHOOK_URL` 后，critical 级告警生成时向该地址 POST 通知（钉钉 text 兼容格式 + 结构化 alert 字段），
fire-and-forget，不阻塞告警主链路。

### 3.7 指令 / OTA

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/devices/{deviceId}/commands` | 下发指令，入参 `{type, payload}`，网关于 MQTT 发布到 `cmd` Topic |
| GET | `/api/devices/{deviceId}/commands` | 指令历史（含回执状态/进度） |
| POST | `/api/ota/tasks` | 创建 OTA 任务，入参 `{deviceIds[], targetVersion, url, description}`；任一设备不存在返回 400 且不创建任务 |
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

### 3.8 统计 / 健康

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/stats/overview` | 设备总数/在线数/今日上报数/未确认告警数 |
| GET | `/api/system/status` | 系统状态快照（仅 admin）：`gateway` 运行时长、`mqtt` 连接与消费量、`storage` 容量、`scheduler` 后台任务参数、`notify` Webhook 是否启用（地址脱敏） |
| GET | `/api/health` | 存活探针：进程存活即 200，不查库/不查 Broker（无需鉴权） |
| GET | `/api/health/ready` | 就绪探针（v1.20.0，v1.23.0 加强）：真实读取 `device` 表 + MQTT 连接状态；未就绪返回 503 与各组件明细（无需鉴权） |
| GET | `/metrics` | Prometheus 文本指标（v1.20.0）：反代只允许内网来源，网关 8000 端口不对外发布 |

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
| 设备列表 | `GET /api/devices` | `page` / `pageSize=20` / `status` / `keyword` / `group` / `hasAlert` / `withAlertCount=true` |
| 分组概览 | `GET /api/devices/groups` | 分组 chips 数据源；空分组与「未分组」合并 |
| 每设备最新值 | `GET /api/records/latest` | `deviceIds=<当前已加载页设备>`，只取已加载设备，避免全量 |
| 单设备历史上报 | `GET /api/devices/{id}/records` | 详情页分页：`limit=11&offset=<已取条数>`，多取 1 条用于判断是否还有更早数据，展示 10 条/页（v1.13.0） |
| 未处理告警 | `GET /api/alerts` | `status=open&page=1&pageSize=20`，`total` 为全量未处理数 |
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

### 4.4 支撑表（OTA / 指令链路必需）

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

### `rule_config` 阈值在线配置表

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| key | varchar(64) PK | 配置键（与 Settings.threshold_* 属性同名） |
| value | float | 阈值数值 |
| updated_by | varchar(64) | 最后修改人 |
| updated_at | timestamptz | 最后修改时间 |

规则引擎读取顺序：`rule_config` 覆盖缓存 → 环境变量 → 代码默认值。网关启动时全量加载，
PUT 接口更新后即时刷新进程内缓存，无需重启。

### `audit_log` 审计日志表

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigserial PK | 自增 |
| operator | varchar(64) | 操作人（登录用户名） |
| action | varchar(32) | 动作：login / command.create / ota.create / alert.ack / threshold.update / user.* / device.* |
| target_type | varchar(32) | 目标类型（device / alert / ota_task / rule_config / app_user） |
| target_id | varchar(64) | 目标标识 |
| detail | jsonb | 动作上下文（如指令类型、阈值变更明细） |
| created_at | timestamptz | 操作时间，索引 |

动作枚举：`login` / `command.create` / `ota.create` / `alert.ack` / `threshold.update` /
`user.create` / `user.update` / `user.delete`。

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
