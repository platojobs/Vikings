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

后续请求头：`Authorization: Bearer <token>`。

### 3.2 设备

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/devices` | 列表，Query：`status`(online/offline)、`keyword`、`page`、`pageSize` |
| GET | `/api/devices/{deviceId}` | 详情 + 最新一条传感器数据 |
| GET | `/api/devices/{deviceId}/records` | 历史数据，Query：`start`、`end`（ISO8601 或毫秒时间戳）、`limit` |
| GET | `/api/devices/{deviceId}/records/aggregate` | 时序聚合，Query：`bucket`(hour/day)、`start`、`end`，返回每桶 count 与三指标 avg/min/max |
| GET | `/api/records/latest` | 全局最新数据（仪表盘首屏兜底） |

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
| GET | `/api/health` | 存活探针（含 Broker 连接状态） |

---

## 4. 数据库表结构

### 4.1 `device` 设备表

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| device_id | varchar(64) PK | 设备唯一 ID |
| name | varchar(128) | 设备名称 |
| model | varchar(64) | 型号 |
| firmware_version | varchar(32) | 当前固件版本 |
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
| action | varchar(32) | 动作：login / command.create / ota.create / alert.ack / threshold.update |
| target_type | varchar(32) | 目标类型（device / alert / ota_task / rule_config） |
| target_id | varchar(64) | 目标标识 |
| detail | jsonb | 动作上下文（如指令类型、阈值变更明细） |
| created_at | timestamptz | 操作时间，索引 |

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
