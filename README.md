# IoT 全链路管理平台（Flutter + Vue3 + Python + MQTT）

面向智能硬件场景的 AIoT 全链路管理平台：**Flutter 设备端**模拟电池传感器硬件，
**Mosquitto** 托管全部设备连接，**Python 异步网关**消费消息、入库并做 AI 异常诊断，
**Vue3 管理后台**通过 MQTT over WebSocket 做实时看板与远程 OTA。

架构目标：贴合真实中小 IoT 公司的生产架构，而不是"学生玩具项目"。

---

## 1. 技术栈

| 层 | 选型 | 说明 |
| --- | --- | --- |
| 设备端 | Flutter 3.x + mqtt_client | 模拟 BLE 电池传感器，间歇上线、休眠省电 |
| 消息总线 | Mosquitto 2.x（MQTT 3.1.1） | 1883 设备 TCP 端口，9001 浏览器 WebSocket 端口 |
| 业务网关 | Python 3.11+ / FastAPI / aiomqtt / SQLAlchemy | 全异步 IO，消息消费、入库、AI 诊断、指令路由 |
| 数据库 | PostgreSQL 16（本地可切 SQLite） | 设备元数据 + 时序数据 + 告警 + 指令/OTA |
| 管理后台 | Vue 3 + TypeScript + Element Plus + ECharts + mqtt.js | 实时看板、历史查询、OTA 管理 |
| AI | OpenAI 兼容 LLM 接口 | 规则判定 + LLM 文案，无 Key 自动降级模板 |

> 本机 Python 说明：macOS 自带的 `python3` 是 **3.9.6**（Xcode 自带，无 pip/venv 且版本过旧），
> 本项目统一使用 Homebrew 的 `/opt/homebrew/bin/python3.14`，脚本已强制指定。

---

## 2. 架构

```
┌──────────────────┐                 ┌────────────────────┐
│ Flutter 设备端    │                 │ Vue3 管理后台       │
│ 模拟电池传感器     │                 │ 实时看板 / OTA      │
└────────┬─────────┘                 └─────────┬──────────┘
         │ MQTT over TCP 1883                   │ MQTT over WebSocket 9001
         │ 唤醒→上报→收指令→休眠                  │ 实时订阅 + REST 查询
         ▼                                      ▼
┌───────────────────────────────────────────────────────────┐
│                  Mosquitto MQTT Broker                    │
│  托管全部设备长连接  ·  Retain 状态  ·  LWT 遗嘱离线判定    │
└──────────────────────────┬────────────────────────────────┘
                           │ 订阅 report / status / ack
                           │ 发布 cmd / system alert
                           ▼
┌───────────────────────────────────────────────────────────┐
│           Python IoT 网关（FastAPI + aiomqtt）             │
│  MQTT 消费器 → 校验 → 入库 → 规则引擎 → AI 文案 → 告警推送  │
│  HTTP 层：JWT 鉴权、历史查询、指令下发、OTA 任务管理         │
└──────────────────────────┬────────────────────────────────┘
                           ▼
                    ┌─────────────────┐
                    │   PostgreSQL    │
                    │ device / sensor_record / device_alert
                    │ device_command / ota_task
                    └─────────────────┘
```

设计要点详见 [docs/architecture.md](docs/architecture.md)，接口契约详见 [docs/contracts.md](docs/contracts.md)。

### 2.1 为什么这样设计

1. **不自己维护 TCP 连接**：设备长连接全部交给 Mosquitto。网关重启不会引发设备重连风暴，
   连接层与业务层可独立扩缩容。
2. **电池设备间歇上线**：真实硬件不会 7×24 在线。设备 `唤醒 → 上线(retained) → 上报 →
   短窗口收指令 → 休眠下线`，并用 **LWT 遗嘱**兜底异常掉电，后台不会出现"假在线"。
3. **指令走 MQTT 下行，不用 HTTP 轮询**：后台创建指令 → 网关落库 → publish 到 `cmd` Topic，
   设备在唤醒窗口内执行并回执 `cmd/ack`，OTA 进度也走同一通道。
4. **浏览器必须用 MQTT over WebSocket**：这是协议栈限制（浏览器无法直接建 TCP MQTT 连接），
   不是技术选型偷懒；实时流走 MQTT，历史批量查询走 REST，两者分工清晰。
5. **Python 够用的理由**：网关是 IO 密集型（等网络、等数据库），GIL 影响有限；
   `aiomqtt` 单进程可稳定消费数百设备；真正瓶颈在 Broker 与数据库写入，不在 Python。

---

## 3. 目录结构

```
.
├── docs/
│   ├── architecture.md        架构设计与决策说明
│   └── contracts.md           三端接口契约（Topic / REST / 表结构 / 阈值）
├── infra/
│   ├── mosquitto/mosquitto.conf   双端口 Broker 配置
│   ├── postgres/init.sql          建表 + 索引 + 种子数据（幂等）
│   └── docker-compose.yml         Broker + PostgreSQL 编排
├── gateway/                   Python IoT 网关
│   ├── app/
│   │   ├── main.py            FastAPI 入口与生命周期
│   │   ├── config.py          环境变量配置
│   │   ├── database.py        异步引擎与会话
│   │   ├── models.py          5 张表的 ORM 模型
│   │   ├── schemas.py         MQTT 报文与 REST 出入参校验
│   │   ├── topics.py          Topic 构造 / 解析（禁止业务代码手写）
│   │   ├── rules.py           告警阈值规则引擎
│   │   ├── security.py        JWT 鉴权
│   │   ├── ai/diagnosis.py    LLM 文案 + 模板降级
│   │   ├── mqtt/              client.py（连接/重连/发布）+ handlers.py（业务分发）
│   │   ├── services/          device / alert / command / stats 四个业务服务
│   │   └── api/routes/        auth / devices / alerts / ota / stats
│   └── requirements.txt
├── web/                       Vue3 + TS 管理后台
├── device_app/                Flutter 设备端（模拟硬件）
├── scripts/
│   ├── start_all.sh           一键启动 Broker + PostgreSQL + 网关
│   ├── start_web.sh           启动前端
│   ├── stop_all.sh            一键停止
│   ├── smoke_test.sh          端到端冒烟验证
│   └── simulate_device.py     无手机时的设备模拟器（联调用）
└── README.md
```

---

## 4. 快速开始

### 4.1 前置依赖

```bash
brew install mosquitto postgresql@16 python@3.14
```

### 4.2 一键启动（推荐）

```bash
# Broker + PostgreSQL + 网关（首次会自动建库建表、装 Python 依赖）
./scripts/start_all.sh

# 管理后台（另开终端，或后台执行）
./scripts/start_web.sh
```

访问入口：

| 服务 | 地址 |
| --- | --- |
| 管理后台 | http://127.0.0.1:5173 |
| 网关 API 文档 | http://127.0.0.1:8000/docs |
| 健康检查 | http://127.0.0.1:8000/api/health |
| MQTT（设备） | 127.0.0.1:1883 |
| MQTT（浏览器） | ws://127.0.0.1:9001 |

默认后台账号：`admin / admin123`（见 `gateway/.env`，生产必须改）。
首次启动还会创建两个演示账号：`operator / operator123`（运维）、`viewer / viewer123`（只读），
用于直接演示角色权限差异。

### 4.3 启动设备端

```bash
cd device_app
flutter pub get
flutter run          # 选择模拟器 / 真机；Broker 地址填本机局域网 IP
```

> Android 模拟器访问宿主机用 `10.0.2.2`，iOS 模拟器可直接用 `127.0.0.1`。

### 4.4 无手机时用模拟器打点

```bash
gateway/.venv/bin/python scripts/simulate_device.py --device-id dev-0001 --count 3 --interval 5
# 注入异常值触发 AI 告警
gateway/.venv/bin/python scripts/simulate_device.py --device-id dev-0001 --count 1 --voltage 2.90 --temperature 78
```

### 4.5 Docker 方式启动基础设施（可选）

```bash
docker compose -f infra/docker-compose.yml up -d
```

---

## 5. 功能清单

### 设备端（Flutter）

- 间歇上线 + 休眠省电状态机，UI 展示当前阶段与倒计时
- 连接即发布 retained `online`，配置 LWT 遗嘱 `offline(lwt)`
- 周期上报温度、湿度、电压、RSSI、电量、seq、时间戳
- 订阅云端指令：校准 / OTA / 重启 / 配置
- OTA 全程保持唤醒，按 10/30/55/80/100 回执进度，完成后更新固件版本
- 事件日志面板，便于现场讲解与排障

### 网关（Python）

- 异步消费三类设备 Topic，指数退避重连，Broker 重启自愈
- 字段级校验 + Topic 与报文 deviceId 一致性校验，脏数据不进库
- 幂等入库：`(device_id, seq, ts)` 唯一约束，QoS1 重复投递自动丢弃
- 告警抑制：同设备同指标 5 分钟窗口内只告警一次
- AI 诊断：规则判定 + LLM 文案，无 Key / 超时自动降级模板
- 指令状态机 `issued → received → progress → success/failed`，禁止状态回退
- OTA 任务汇总（成功/失败计数）与设备固件版本同步
- 账号体系与 RBAC：admin / operator / viewer 三角色，PBKDF2 口令哈希 + JWT
- 设备全生命周期：手工注册、元数据编辑、删除保护、批量注册（重复 ID 跳过）
- 设备分组：分组维度筛选与在线/离线统计，支撑按产线批量运维
- 管理动作全量审计：登录、指令、OTA、告警确认、阈值修改、账号与设备变更
- 阈值在线配置：`rule_config` 持久化覆盖环境变量，改完即时生效
- 时序数据按小时/天聚合接口，支撑长周期趋势视图
- critical 告警 Webhook 外推（钉钉/飞书/企业微信机器人兼容）
- JWT 鉴权 + 统一 `{code, message, data}` 响应体

### 管理后台（Vue3）

- 登录页 + 路由守卫 + 角色驱动的菜单与按钮权限
- 实时仪表盘：指标卡、多设备实时曲线、实时告警流
- 设备列表：状态 / 分组 / 关键字筛选，注册与编辑、批量导入（CSV 文件或粘贴）、
  导出 CSV、删除保护，详情抽屉（历史曲线 + 指令历史 + 指令下发）
- 历史查询：明细 / 按小时 / 按天聚合视图，CSV 导出
- 告警中心：统计卡片、实时告警推送、在线确认、阈值配置抽屉
- 审计日志：动作/操作人筛选，变更详情可查看
- 账号管理：新建账号、角色调整、启停、重置口令、删除保护
- OTA 管理：创建任务、任务列表、逐设备升级进度实时刷新

---

## 6. MQTT Topic 与 REST API

### Topic（详见 docs/contracts.md）

| Topic | 方向 | 说明 |
| --- | --- | --- |
| `device/{id}/sensor/report` | 设备 → 云 | 传感器上报，QoS1 |
| `device/{id}/status` | 设备 → 云 | 上下线，QoS1 + Retain + LWT |
| `device/{id}/cmd` | 云 → 设备 | 校准 / OTA / 重启 / 配置 |
| `device/{id}/cmd/ack` | 设备 → 云 | 指令回执与 OTA 进度 |
| `device/system/alert` | 网关 → 后台 | AI 告警消息 |

### REST（前缀 `/api`，除登录与健康检查外均需 Bearer Token）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | 登录换取 JWT |
| GET | `/api/auth/me` | 当前账号最新信息（角色刷新） |
| GET | `/api/devices` | 设备列表（status / keyword / group / 分页） |
| GET | `/api/devices/groups` | 分组概览（总数 / 在线 / 离线） |
| POST | `/api/devices` | 注册设备 |
| POST | `/api/devices/bulk` | 批量注册（重复跳过） |
| PATCH/DELETE | `/api/devices/{id}` | 编辑元数据 / 删除（删除仅 admin） |
| GET | `/api/devices/{id}` | 设备详情 + 最新数据 |
| GET | `/api/devices/{id}/records` | 历史数据（start / end / limit） |
| GET | `/api/devices/{id}/records/aggregate` | 按小时/天聚合（avg/min/max） |
| GET | `/api/records/latest` | 全局最新数据（看板首屏兜底） |
| GET | `/api/alerts` | 告警列表（deviceId / level / status） |
| POST | `/api/alerts/{id}/ack` | 告警确认 |
| GET | `/api/alerts/summary` | 告警统计 |
| GET/PUT | `/api/rules/thresholds` | 阈值查询 / 在线更新 |
| GET | `/api/audit-logs` | 审计日志（operator / action / 分页） |
| GET/POST | `/api/users` | 账号列表 / 新建（仅 admin） |
| PATCH/DELETE | `/api/users/{username}` | 修改角色与启停 / 删除（仅 admin） |
| POST | `/api/devices/{id}/commands` | 下发指令 |
| GET | `/api/devices/{id}/commands` | 指令历史与回执状态 |
| POST | `/api/ota/tasks` | 创建 OTA 任务 |
| GET | `/api/ota/tasks` | 任务列表（含逐设备状态） |
| GET | `/api/ota/tasks/{id}` | 任务详情 |
| GET | `/api/stats/overview` | 设备/上报/告警统计 |
| GET | `/api/health` | 存活探针（含 Broker 连接状态） |

---

## 7. 数据库

| 表 | 作用 | 关键设计 |
| --- | --- | --- |
| `device` | 设备元数据 | `status` 由 MQTT retained/LWT 驱动，`group_name` 支撑分组运维 |
| `sensor_record` | 时序数据 | `(device_id, ts DESC)` 查询索引；`(device_id, seq, ts)` 唯一索引做幂等 |
| `device_alert` | AI 告警 | `(device_id, metric, created_at)` 索引支撑告警抑制 |
| `device_command` | 指令与回执状态 | 状态机 + OTA 进度持久化 |
| `ota_task` | OTA 任务汇总 | 一次任务覆盖多设备，汇总成功/失败数 |
| `rule_config` | 阈值在线覆盖 | 优先于环境变量，改完即时生效 |
| `audit_log` | 管理动作审计 | 动作 + 目标 + JSON 上下文，按时间索引 |
| `app_user` | 后台账号与角色 | PBKDF2 口令哈希，角色驱动接口权限 |

建表脚本：[infra/postgres/init.sql](infra/postgres/init.sql)，网关启动时也会幂等补齐缺失表。

---

## 8. AI 诊断

```
传感器上报 → 规则引擎判定（电压/温度双向/湿度/RSSI）
          → 命中则把数值 + 阈值 + 级别交给 LLM
          → 生成面向运维的处置文案（80 字内，含具体动作）
          → 落库 device_alert → publish device/system/alert
```

- 规则与文案分离：规则可测试、可配置；LLM 只负责表达与处置建议。
- 未配置 `LLM_API_KEY`、超时或返回异常时，自动降级为内置模板文案，链路永不中断。
- 阈值：电压 <3.4V 预警 / <3.2V 严重；温度 >60℃ 预警 / >75℃ 严重（低温对称）；
  湿度 >90% 预警 / >97% 严重；RSSI <-100dBm 预警 / <-110dBm 严重。

---

## 9. 3 分钟演示流程

1. `./scripts/start_all.sh` → 打开 `http://127.0.0.1:8000/api/health` 展示 Broker 已连接。
2. 启动 Flutter App：展示「休眠 → 唤醒 → 上线 → 上报 → 休眠」循环与倒计时。
3. 打开后台仪表盘：曲线随设备上报实时跳动，设备状态在在线/离线间切换（间歇上线可见）。
4. 用模拟器注入 `--voltage 2.90 --temperature 78`：后台实时弹出 critical 告警，展示 AI 文案。
5. 后台设备详情下发 calibrate：App 日志显示收到指令并回执，指令历史状态变为 success。
6. 创建 OTA 任务：后台进度条实时推进到 100%，设备固件版本同步更新。
7. 用 `viewer / viewer123` 重新登录：下发指令、确认告警、阈值配置全部隐藏，接口直接返回 403。
8. 关闭 App（模拟掉电）：Mosquitto 的 LWT 遗嘱立即把设备置为离线，证明状态判定不依赖心跳超时。

---

## 10. 性能与扩容

- 单网关协程模型实测可稳定消费数百设备上报；瓶颈在 Broker 吞吐与数据库写入。
- 扩容路径：`uvicorn --workers` 多进程 → 多实例 + Nginx → 消息队列削峰 → 迁移 Go 网关。
- 代码按「消费器 / 服务层 / 存储层」分层，迁移时业务逻辑可平移。
- 时序表建议按 `device_id` 分区或引入 TimescaleDB；当前规模单表 + 复合索引足够。

---

## 11. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 网关报 `permission denied for table device` | init.sql 用了超级用户执行，表归属错误。用 `iot` 账号执行 init.sql，或 `ALTER TABLE ... OWNER TO iot` |
| `python3` 是 3.9.6 | macOS/Xcode 自带版本；用 `/opt/homebrew/bin/python3.14`，脚本已内置 |
| 网关 `/api/health` 显示 `mqttConnected:false` | Broker 未启动或端口不符，查看 `logs/gateway.log` 与 `logs/mosquitto.log` |
| 浏览器连不上 9001 | 确认 mosquitto.conf 已配置 `protocol websockets`，并检查防火墙 |
| 设备一直离线 | 检查 LWT 报文、Topic 拼写；模拟器用 `--count 1` 时上报后会立即休眠，属正常现象 |
| 告警只有模板文案 | 未配置 `LLM_API_KEY`，属预期降级行为 |

---

## 12. 版本记录

| Tag | 内容 |
| --- | --- |
| `v0.1.0-contracts` | 架构与三端接口契约基线 |
| `v0.2.0-infra` | Mosquitto 双端口、PostgreSQL 建表脚本、容器编排与依赖 |
| `v0.3.0-gateway` | Python 异步网关全链路（消费/入库/AI 告警/指令/OTA） |
| `v0.4.0-tooling` | 一键启停脚本与端到端冒烟测试 |
| `v0.5.0-web` | Vue3 管理后台（实时看板/设备/历史/OTA） |
| `v0.6.0-device` | Flutter 设备端间歇上线状态机与指令回执 |
| `v1.0.0` | 全链路联调通过 + 完整文档 |
| `v1.1.0` | 离线巡检定时任务、时序数据保留清理、告警阈值可配置、后端单测、GitHub Actions CI、前端实时状态刷新 |
| `v1.2.0` | 设备指令下发面板、cmd/ack 实时回执、CSV 导出、ECharts 按需引入（主包 1.41MB→685KB） |
| `v1.3.0` | 告警中心、阈值在线配置、审计日志、时序聚合接口、critical 告警 Webhook |
| `v1.4.0` | 账号体系与 RBAC（三角色）、账号管理页、按钮级权限收口、登录页演示账号 |
| `v1.5.0` | 设备全生命周期（注册/编辑/删除保护/批量导入）、设备分组与统计、设备列表 CSV 导出 |
