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

设计说明：

- `{deviceId}` 为设备唯一标识，全链路统一使用同一字符串（如 `dev-0001`）。
- 状态 Topic 使用 **Retain + LWT（Last Will and Testament）**：设备上线发布 `online`（retained），
  Broker 在设备异常掉线时自动代为发布遗嘱 `offline`，网关无需依赖心跳超时才判定离线。
- 指令回执 `cmd/ack` 是原需求「OTA 进度反馈」的标准化承载方式，避免为 OTA 单独开一类 Topic。

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
- `progress`：0 ~ 100，仅 OTA 场景有值

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

---

## 3. REST API（Python 网关，前缀 `/api`）

统一响应体：

```json
{ "code": 0, "message": "ok", "data": {} }
```

`code != 0` 表示业务失败，HTTP 状态码同时给出（400/401/404/500）。

### 3.1 认证

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | 登录，入参 `{username, password}`，返回 `{token, user:{username, role}}` |
| GET | `/api/auth/me` | 返回数据库中的最新账号信息（角色被调整后前端刷新即可生效） |

后续请求头：`Authorization: Bearer <token>`。登录凭证校验数据库 `app_user` 表，
口令以 PBKDF2-SHA256 哈希存储。角色与权限矩阵：

| 角色 | 权限 |
| --- | --- |
| `admin` | 全部：设备/告警/OTA/审计查询 + 阈值配置 + 账号管理 |
| `operator` | 查询 + 下发指令 + 创建 OTA 任务 + 确认告警 |
| `viewer` | 只读：仅设备/告警/OTA/审计查询 |

越权访问返回 HTTP 403，未登录或令牌失效返回 401。

### 3.2 设备

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/devices` | 列表，Query：`status`(online/offline)、`keyword`、`group`、`hasAlert`、`withAlertCount`、`page`、`pageSize` |
| GET | `/api/devices/groups` | 分组概览：每组 `total` / `online` / `offline` |
| POST | `/api/devices` | 手工注册设备，入参 `device_id`（3~64 位字母数字 _-）、`name`、`model`、`firmware_version`、`group_name`；重复 ID 返回 409 |
| POST | `/api/devices/bulk` | 批量注册，单次最多 200 台，返回 `{created, skipped}`，重复 ID 跳过 |
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

写操作权限：注册 / 批量注册 / 编辑为 operator+admin，删除为 admin。

### 3.3 告警

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/alerts` | 列表，Query：`deviceId`、`level`(warning/critical)、`status`(open/acked)、`page`、`pageSize` |
| POST | `/api/alerts/{alertId}/ack` | 告警确认 |
| GET | `/api/alerts/summary` | 告警统计：`{open, critical_open, today}` |

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
| POST | `/api/ota/tasks` | 创建 OTA 任务，入参 `{deviceIds[], targetVersion, url, description}` |
| GET | `/api/ota/tasks` | OTA 任务列表（含每台设备升级状态） |
| GET | `/api/ota/tasks/{taskId}` | OTA 任务详情 |

### 3.8 统计 / 健康

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/stats/overview` | 设备总数/在线数/今日上报数/未确认告警数 |
| GET | `/api/system/status` | 系统状态快照（仅 admin）：`gateway` 运行时长、`mqtt` 连接与消费量、`storage` 容量、`scheduler` 后台任务参数、`notify` Webhook 是否启用（地址脱敏） |
| GET | `/api/health` | 存活探针（含 Broker 连接状态，无需鉴权） |

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
| 登录 | `POST /api/auth/login` | `{username, password}` → `data: {token, user}` |
| 设备列表 | `GET /api/devices` | `page` / `pageSize=20` / `status` / `keyword` / `group` / `hasAlert` / `withAlertCount=true` |
| 分组概览 | `GET /api/devices/groups` | 分组 chips 数据源；空分组与「未分组」合并 |
| 每设备最新值 | `GET /api/records/latest` | `deviceIds=<当前已加载页设备>`，只取已加载设备，避免全量 |
| 单设备历史上报 | `GET /api/devices/{id}/records` | 看板固定 `limit=10` |
| 未处理告警 | `GET /api/alerts` | `status=open&page=1&pageSize=20`，`total` 为全量未处理数 |
| 指令回执 | `GET /api/devices/{id}/commands` | `limit=20` |
| 汇总卡 | `GET /api/stats/overview` | `device_total` / `device_online` / `alert_open` 等服务端全量统计 |

统一鉴权头 `Authorization: Bearer <token>`；token 由登录接口下发，与 Web 后台一致。
看板对 `401` 的处理：清除本地 token 并回到登录页；`403` 提示无权访问；
超时与网络错误展示可读文案。v1.9.0 起额外调用 3.10.2 列出的两个写接口，
写权限与 403 处理不复制 RBAC，直接沿用登录会话角色与网关响应。

#### 3.10.1 移动看板实时订阅（v1.8.0）

v1.8.0 起看板增量数据改走 MQTT over WebSocket（默认 `ws://<网关主机>:9001`），
**不新增 Topic、不修改报文格式**，仅订阅既有 Topic：

| 用途 | Topic | 说明 |
| --- | --- | --- |
| 新告警 | `device/system/alert` | 报文见 2.5（camelCase），按 `alertId` 去重后插入头条 |
| 设备上报 | `device/+/sensor/report` | 报文见 2.1，按 `deviceId` 覆盖最新值并标记在线 |
| 设备上下线 | `device/+/status` | 报文见 2.2，优先级高于 REST 快照，LWT 遗嘱离线同样生效 |

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
| 告警确认 | `POST /api/alerts/{alertId}/ack` | 无 body | `data: AlertOut`（`status=acked`） | operator / admin |

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

### 4.4 支撑表（OTA / 指令链路必需）

### `device_command`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| cmd_id | varchar(64) PK | 指令 ID |
| device_id | varchar(64) | 目标设备 |
| type | varchar(16) | calibrate / ota / reboot / config |
| payload | jsonb | 指令参数 |
| state | varchar(16) | issued / received / progress / success / failed |
| progress | int | 0 ~ 100 |
| message | varchar(255) | 回执描述 |
| ota_task_id | varchar(64) NULL | 归属 OTA 任务 |
| operator | varchar(64) | 下发人 |
| created_at / updated_at | timestamptz | 时间戳 |

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

首次启动且表为空时，网关按环境变量 `ADMIN_USERNAME/ADMIN_PASSWORD` 初始化管理员，
并额外创建 `operator/operator123` 与 `viewer/viewer123` 两个演示账号；
已有账号时不再覆盖，避免重启重置运维改过的口令。

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
