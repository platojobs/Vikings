# 架构设计说明

## 1. 定位

智能硬件 IoT 全链路管理平台：Flutter 设备端（模拟 BLE 电池传感器）+ Mosquitto MQTT Broker +
Python 异步业务网关 + PostgreSQL + Vue3 管理后台，内置 AI 异常诊断。

目标场景：中小型智能硬件公司（设备量 10² ~ 10³），典型的"设备—云端—后台"三段式架构。

## 2. 全链路拓扑

```
┌────────────────────┐            ┌──────────────────────┐
│ Flutter 设备端      │            │ Vue3 管理后台         │
│（模拟电池传感器硬件）│            │ Element Plus + ECharts│
└─────────┬──────────┘            └──────────┬───────────┘
          │ MQTT over TCP (1883)              │ MQTT over WebSocket (9001)
          │ 间歇上线、休眠省电                 │ 实时订阅 + REST 查询
          ▼                                   ▼
┌──────────────────────────────────────────────────────────┐
│              Mosquitto MQTT Broker（连接托管层）           │
│  · 承载全部设备长连接    · Retain / LWT 自动上下线判定       │
│  · 1883 设备端口         · 9001 WebSocket 端口             │
└───────────────────────────┬──────────────────────────────┘
                            │ 订阅 device/*/sensor/report、device/*/status、device/*/cmd/ack
                            │ 发布 device/*/cmd、device/system/alert
                            ▼
┌──────────────────────────────────────────────────────────┐
│           Python IoT 网关（FastAPI + aiomqtt）            │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────────────┐ │
│  │ MQTT 消费器  │ │ 业务服务层   │ │ AI 诊断模块          │ │
│  │ 异步协程     │ │ 设备/告警/OTA│ │ 规则引擎 + LLM 文案   │ │
│  └─────────────┘ └─────────────┘ └─────────────────────┘ │
│  ┌─────────────────────────────────────────────────────┐ │
│  │ FastAPI HTTP 层（JWT 鉴权 / 历史查询 / 指令下发）      │ │
│  └─────────────────────────────────────────────────────┘ │
└───────────────────────────┬──────────────────────────────┘
                            ▼
                   ┌──────────────────┐
                   │  PostgreSQL      │
                   │  device          │
                   │  sensor_record   │
                   │  device_alert    │
                   │  device_command  │
                   │  ota_task        │
                   └──────────────────┘
```

## 3. 分层职责

| 层 | 技术 | 职责 | 不做什么 |
| --- | --- | --- | --- |
| 连接层 | Mosquitto | 维护设备 TCP 长连接、订阅关系、Retain/LWT | 不做业务逻辑 |
| 业务层 | Python 网关 | 消息消费、校验、入库、告警判定、指令路由、HTTP 接口 | 不维护任何设备连接 |
| 存储层 | PostgreSQL | 设备元数据、时序数据、告警、指令与 OTA 任务 | 不做实时推送 |
| 展示层 | Vue3 | 实时看板、历史查询、OTA 管理 | 不直连数据库 |

## 4. 关键设计决策（面试可讲点）

### 4.1 不自己维护 TCP 连接

设备长连接全部托管给专业 MQTT Broker。业务网关只订阅 Topic，不做任何 socket 管理，
网关重启不会导致设备掉线重连风暴，连接层与业务层可独立扩缩容。

### 4.2 电池设备"间歇上线 + 休眠省电"

真实电池硬件不会 7×24 在线。设备端实现：

```
唤醒 → 连接 Broker → 发布 retained online → 上报一次数据 →
订阅 cmd（短窗口接收云端指令）→ 发布 offline(sleep) → 断开 → 休眠 N 秒 → 循环
```

配套用 LWT 兜底：设备异常掉电时 Broker 自动发布 `offline(lwt)`，后台状态不会"假在线"。

### 4.3 指令下发走 MQTT 下行，而不是 HTTP 轮询

后台创建指令 → 网关持久化 `device_command` → 网关 publish 到 `device/{id}/cmd`。
设备在唤醒窗口内收到指令并回执 `cmd/ack`；OTA 进度也通过 `cmd/ack` 的 `progress` 字段上报，
后台通过 MQTT over WebSocket 实时看到进度条变化。

### 4.4 浏览器用 MQTT over WebSocket

浏览器无法直接建 TCP MQTT 连接，这是协议栈限制而非设计偷懒。Mosquitto 开放 9001 端口作为
WebSocket 监听，前端用 mqtt.js 订阅 `device/*/sensor/report`、`device/system/alert` 实现毫秒级实时看板；
历史数据仍走 REST，二者分工明确（实时流走 MQTT，批量查询走 HTTP）。

### 4.5 为什么 Python 够用

- 网关是 **IO 密集型**（等网络、等数据库），不是 CPU 密集型，GIL 影响有限。
- `aiomqtt` 基于 asyncio，单进程单协程即可稳定消费数百设备的上报消息。
- 真正的瓶颈在 Broker 的消息吞吐与数据库写入，而不是 Python 解释器。
- 扩容路径：`uvicorn --workers` 多进程 → 多实例 + Nginx 负载 → 引入消息队列削峰 → 迁移 Go 网关。
  业务代码按「消费器 / 服务层 / 存储层」分层，迁移成本可控。

### 4.6 AI 诊断的位置

内置在网关的告警链路中，属于"规则判定 + LLM 文案生成"的混合方案：
规则负责确定性触发（阈值可配置、可测试），LLM 负责把冷冰冰的数值转成运维可执行的处置建议。
未配置 LLM Key 时自动降级为模板文案，保证链路永远可用。

## 5. 数据流

### 上报链路

```
设备 publish sensor/report
  → 网关消费 → Pydantic 校验 → 写 sensor_record
  → 更新 device.last_report_at / status=online
  → 规则引擎判定 → 命中则 AI 生成文案 → 写 device_alert
  → publish device/system/alert
  → 后台 WebSocket 实时刷新图表与告警
```

### 指令链路

```
后台 POST /api/devices/{id}/commands
  → 网关写 device_command(state=issued)
  → publish device/{id}/cmd
  → 设备回执 cmd/ack(received/progress/success)
  → 网关更新 device_command + WebSocket 实时展示
```

## 6. 目录结构

```
.
├── docs/                 架构与契约文档
├── infra/                Mosquitto 配置、SQL 初始化、docker-compose
├── gateway/              Python IoT 网关（FastAPI + aiomqtt + SQLAlchemy）
├── web/                  Vue3 + TS 管理后台
├── device_app/           Flutter 设备端（模拟硬件）
└── scripts/              一键启动 / 一键停止 / 冒烟测试
```

## 7. 可靠性设计

- 消息消费幂等：`sensor_record` 以 `(device_id, seq, ts)` 做去重判断，重复投递不产生脏数据。
- 告警抑制：同设备同指标 5 分钟窗口内只产生一条告警，避免抖动刷屏。
- MQTT 断线重连：网关与设备端均实现指数退避重连，Broker 重启后自动恢复。
- LWT：设备异常掉线由 Broker 代发遗嘱，状态判定不依赖网关存活。
