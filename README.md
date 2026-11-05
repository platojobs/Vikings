# IoT 全链路管理平台（Flutter + Vue3 + Python + MQTT）

面向智能硬件场景的 AIoT 全链路管理平台：**Flutter 设备端**模拟电池传感器硬件，
**Mosquitto** 托管全部设备连接，**Python 异步网关**消费消息、入库并做 AI 异常诊断，
**Vue3 管理后台**通过 MQTT over WebSocket 做实时看板与远程 OTA。

架构目标：贴合真实中小 IoT 公司的生产架构，而不是"学生玩具项目"。

> **生产部署（v1.22.0）**：一台干净机器执行 `./scripts/prod_up.sh`，
> 即可构建并拉起 PostgreSQL + Mosquitto + 网关 + Web + HTTPS 反代（含健康检查、数据卷与网络隔离）；
> 表结构由 Alembic 迁移自动升级，备份/恢复脚本见 `scripts/pg_backup.sh` / `scripts/pg_restore.sh`。
> 完整步骤、端口、证书替换、升级备份与人工验收命令见 **[docs/deployment.md](docs/deployment.md)**；
> 日常启停、迁移流程、日志、凭证轮换、保留策略与回滚见 [docs/operations.md](docs/operations.md)；
> 日志字段、`/ready` 健康分层、Prometheus 指标与告警阈值见 **[docs/observability.md](docs/observability.md)**；
> 移动端出包（keystore/签名、构建号、CI 制品、依赖治理）见 **[docs/mobile-release.md](docs/mobile-release.md)**；
> 发布前真机验收（接入、凭证、指令、OTA、告警、弱网、省电、异常文案）见 **[docs/device-acceptance.md](docs/device-acceptance.md)**。

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

设计要点详见 [docs/architecture.md](docs/architecture.md)，接口契约详见 [docs/contracts.md](docs/contracts.md)，
交付就绪审计与 P0/P1/P2 分级清单详见 [docs/delivery-readiness.md](docs/delivery-readiness.md)。

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
│   ├── contracts.md           三端接口契约（Topic / REST / 表结构 / 阈值）
│   ├── mobile-release.md      Android 发布：签名、构建号、CI 制品与依赖治理
│   └── device-acceptance.md   发布前真机手工验收清单（可执行步骤与通过标准）
├── infra/
│   ├── mosquitto/                 双端口 Broker 配置 + ACL
│   ├── postgres/init.sql          建表 + 索引 + 种子数据（幂等）
│   ├── docker-compose.yml         本地开发依赖编排
│   └── docker-compose.prod.yml    生产全栈编排（反代 / HTTPS / 健康检查）
├── infra/prod/                    生产 env 模板、nginx、Broker 配置、密钥目录（gitignored）
├── gateway/                   Python IoT 网关
│   ├── app/
│   │   ├── main.py            FastAPI 入口与生命周期
│   │   ├── config.py          环境变量配置
│   │   ├── database.py        异步引擎与会话
│   │   ├── models.py          8 张表的 ORM 模型
│   │   ├── schemas.py         MQTT 报文与 REST 出入参校验
│   │   ├── topics.py          Topic 构造 / 解析（禁止业务代码手写）
│   │   ├── rules.py           告警阈值规则引擎
│   │   ├── security.py        JWT 鉴权
│   │   ├── ai/diagnosis.py    LLM 文案 + 模板降级
│   │   ├── mqtt/              client.py（连接/重连/发布）+ handlers.py（业务分发）
│   │   ├── services/          device / alert / command / stats / system 五个业务服务
│   │   └── api/routes/        auth / devices / alerts / ota / stats / system
│   └── requirements.txt
├── web/                       Vue3 + TS 管理后台
├── device_app/                Flutter 设备端（设备模拟器 + 移动看板）
│   └── lib/
│       ├── config/            设备状态机与网关地址配置
│       ├── models/            MQTT 报文与 REST 视图模型
│       ├── pages/             设备模拟器 / 登录 / 移动看板 / 告警中心 / 三模式入口
│       ├── services/          MQTT 客户端、实时订阅、REST 客户端与安全存储
│       └── widgets/           传感器卡片、在线徽标
├── scripts/
│   ├── start_all.sh           一键启动 Broker + PostgreSQL + 网关
│   ├── start_web.sh           启动前端
│   ├── stop_all.sh            一键停止
│   ├── smoke_test.sh          端到端冒烟验证
│   ├── e2e/                   真实 Broker 端到端验收（脚本 + 说明）
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
| 管理后台 | http://127.0.0.1:5173（默认仅本机，手机访问见 4.6） |
| 网关 API 文档 | http://127.0.0.1:8000/docs |
| 健康检查 | http://127.0.0.1:8000/api/health |
| MQTT（设备） | 127.0.0.1:1883 |
| MQTT（浏览器） | ws://127.0.0.1:9001 |

默认后台账号：`admin / admin123`（见 `gateway/.env`，生产必须改）。
开发/测试环境首次启动还会创建两个演示账号：`operator / operator123`（运维）、
`viewer / viewer123`（只读），用于直接演示角色权限差异；生产环境默认**不创建**演示账号。

#### 4.2.1 生产部署前的必改项（v1.16.0）

`APP_ENV=production` 时，网关启动会先校验配置；只要仍有默认/弱密钥、弱口令或 SQLite，
就打印具体原因并**拒绝启动**。开发环境允许默认值，但启动日志会出现「安全配置提醒」。

| 必改项 | 环境变量 | 要求 | 示例 / 命令 |
| --- | --- | --- | --- |
| 运行环境 | `APP_ENV` | 生产必须为 `production` | `APP_ENV=production` |
| JWT 密钥 | `JWT_SECRET` | ≥ 32 位随机值，且不能是仓库公开的默认值 | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| 管理员口令 | `ADMIN_PASSWORD` | ≥ `PASSWORD_MIN_LENGTH`（默认 8），且不在常见弱口令表 | 手工设置强口令，不要沿用 `admin123` |
| 数据库 | `DATABASE_URL` | 必须 PostgreSQL，且口令 ≥ 8 位、不能是 `iot_pass` | `postgresql+asyncpg://iot:<强口令>@db:5432/iot` |
| 演示账号 | `SEED_DEMO_USERS` | 生产默认不种子；仅在明确需要演示时设为 `true` | 留空（推荐） |
| 登录限流 | `LOGIN_MAX_FAILURES` / `LOGIN_FAILURE_WINDOW_SECONDS` / `LOGIN_LOCKOUT_SECONDS` | 默认 5 次 / 300 秒窗口 / 锁定 900 秒 | 按暴露面调整 |

说明：

- 已有账号表非空时不会再写入任何种子账号，升级不会让现存账号消失，也不会重置运维改过的口令。
- 限流按「直连客户端 IP + 用户名」在**进程内**计数，成功登录清零；多进程/多副本部署时各进程独立计数，
  真实阈值会被放大，生产建议同时在反向代理层做限流。
- 新建/重置账号的口令强度校验只影响后续写入，既有账号继续可登录。

#### 4.2.2 Broker 鉴权与凭证管理（v1.17.0）

1883 与 9001 **都已关闭匿名**（`allow_anonymous false` + `per_listener_settings true`），
陌生连接会收到 CONNACK 拒绝；设备只能操作自己的 Topic 子树，Web/移动端只读。
口令与 ACL 的完整矩阵见 [docs/contracts.md](docs/contracts.md) 的「1.1 Broker 鉴权与主体—Topic 矩阵」。

**首次初始化**（`start_all.sh` 已自动执行；手工/Docker 部署需先做）：

```bash
# 生成 infra/mosquitto/passwd（gitignore）与 .run/mqtt/credentials.json（0600，gitignore）
python scripts/mqtt_credentials.py init \
  --device-id dev-0001 --device-id dev-0002
```

口令文件使用 PBKDF2-SHA512，盐固定 12 字节：Ubuntu 自带的 Mosquitto 2.0.x
只接受 12 字节盐，2.1.x 同时接受 12/64，因此该格式可在两者间迁移。

**签发 / 轮换 / 吊销设备凭证**（用户名恒等于 `deviceId`）：

```bash
python scripts/mqtt_credentials.py issue  --subject device:dev-0001 --reload
python scripts/mqtt_credentials.py rotate --subject device:dev-0001 --reload
python scripts/mqtt_credentials.py revoke --subject device:dev-0001 --reload
python scripts/mqtt_credentials.py show          # 仅本机调试：查看状态文件中的口令
```

也可以登录后台调用 `POST /api/devices/{deviceId}/mqtt-credential` 完成首次签发或轮换；
响应中的口令只返回一次，平台不保存明文（`device` 表只有版本 / 指纹 / 时间）。

v1.26.0 起运维可直接在 Web / 移动端设备详情查看凭证状态（是否签发、版本、最近轮换、是否吊销、
指纹前 8 位），并受控轮换/吊销：首次签发 operator 可用，**轮换/吊销仅 admin**。
界面会要求二次确认并只展示一次口令；若 Broker 未重载，会明确提示「已写入新凭证但 Broker 未重载，
旧凭证可能仍然有效」并给补救步骤。操作步骤与补救见
[docs/operations.md](docs/operations.md#313-轮换后提示broker-未重载怎么办)。

**重载 Broker**（修改 `passwd` 后必须执行，否则旧口令仍有效）：

```bash
# 本地 start_all 托管
kill -HUP "$(cat .run/mosquitto.pid)"
# Docker
docker compose -f infra/docker-compose.yml kill -s HUP mosquitto
# 或给网关配置 MQTT_RELOAD_COMMAND，让它签发后自动重载
```

**既有部署升级步骤**：

1. 执行 `python scripts/mqtt_credentials.py init --device-id <每台存量设备>`（或在后台逐台签发）；
2. 在网关 `.env` 设置 `MQTT_PASSWORD`、`MQTT_WEB_PASSWORD`、`MQTT_MOBILE_PASSWORD`
   （网关身份保持 `MQTT_USERNAME=iot-gateway`），并设置 `MQTT_PASSWORD_FILE` 指向 `passwd`；
3. 重启 Broker 与网关（首次从匿名切到鉴权，建议完整重启而非热重载）；
4. Web/移动端用户重新登录，客户端会通过 `/api/auth/mqtt-credentials` 换取只读凭证；
5. 设备端把签发口令写入设备模拟页的「MQTT 口令」，或以 `MQTT_DEVICE_PASSWORD` 注入模拟器。

**故障排查**：

| 现象 | 原因 / 处理 |
| --- | --- |
| Broker 启动报 `Unable to open pwfile "passwd"` | 尚未初始化；先执行 `mqtt_credentials.py init`，并确认工作目录是 `infra/mosquitto/` |
| 网关 `/api/health` 显示 `mqttConnected:false` | `MQTT_USERNAME` 不是 `iot-gateway`、`MQTT_PASSWORD` 缺失或与 `passwd` 不一致 |
| 设备连接被拒（CONNACK 5） | 用户名必须等于 `deviceId`，口令必须是该设备最近一次签发/轮换的结果 |
| Web/移动端实时不了、REST 正常 | `/api/auth/mqtt-credentials` 返回 503 说明服务端未配置 `MQTT_WEB_PASSWORD` / `MQTT_MOBILE_PASSWORD` |
| 轮换后旧口令仍能连 | Broker 未重载；执行 SIGHUP 或设置 `MQTT_RELOAD_COMMAND` |
| 后台签发返回 503 | 网关进程对 `infra/mosquitto/` 无写权限；改用命令行脚本在宿主机签发后重载 |

安全约定：`infra/mosquitto/passwd`、`.run/mqtt/credentials.json` 均不入库；仓库只保留
`mosquitto.conf`、ACL 与生成脚本。文档里的口令一律是占位符，禁止照抄上线。

### 4.3 启动设备端

```bash
cd device_app
flutter pub get
flutter run          # 选择模拟器 / 真机
```

App 启动后底部有四个入口，默认进入「概览」（v1.27.0）：

| 入口 | 用途 | 依赖 |
| --- | --- | --- |
| 概览（默认） | 设备总数 / 在线率 / 未签发凭证、未处理告警级别分布、近 24 小时趋势、异常设备列表（可点进详情）；顶部显示实时连接或轮询降级状态 | 依赖网关 REST（8000）；实时数据复用告警中心的前台订阅（9001） |
| 看板 | 登录网关后查看真实数据（设备/实时值/历史/告警/指令回执），实时增量走 WebSocket；admin / operator 可下发指令、确认告警 | 依赖网关 REST（8000）与 MQTT over WebSocket（9001） |
| 告警中心 | 设备远程搜索筛选 / 滚动分页 / 多选批量确认（>100 条自动分批）；实时新告警入顶，tab 角标显示未读，进入页面清零 | 依赖网关 REST（8000）与 MQTT over WebSocket（9001） |
| 工具 | 设备模拟器：间歇上线、周期上报、收指令、OTA（v1.27.0 起从默认 tab 移到这里，功能不变） | 只依赖 MQTT Broker（1883） |

设备模拟页的「Broker 地址」按运行环境填写，填错会表现为一直连不上：

| 运行环境 | Broker 地址 | 说明 |
| --- | --- | --- |
| iOS 模拟器 | `127.0.0.1` | 模拟器与 Mac 共用网络栈 |
| Android 模拟器 | `10.0.2.2` | AVD 访问宿主机的固定别名 |
| iOS / Android 真机 | 电脑局域网 IP（如 `192.168.1.8`） | 手机与电脑必须在同一 Wi-Fi，且 1883 端口放行 |

> `127.0.0.1` 在真机上指的是手机自己，不是电脑，这是真机连不上 Broker 的最常见原因。

设备模拟页新增「MQTT 口令」输入（Broker 已禁用匿名）：口令取 4.2.2 中该设备最近一次签发/轮换的结果，
用户名固定等于设备 ID。移动看板与告警中心的只读凭证由登录接口自动换取，无需手工填写。

### 4.4 无手机时用模拟器打点

```bash
# 口令来自 4.2.2 的签发/轮换输出；不提供口令会直接报错，不会匿名连接
MQTT_DEVICE_PASSWORD='<dev-0001 的设备口令占位>' \
  gateway/.venv/bin/python scripts/simulate_device.py --device-id dev-0001 --count 3 --interval 5
# 注入异常值触发 AI 告警
MQTT_DEVICE_PASSWORD='<dev-0001 的设备口令占位>' \
  gateway/.venv/bin/python scripts/simulate_device.py --device-id dev-0001 --count 1 --voltage 2.90 --temperature 78
```

### 4.5 Docker 方式启动基础设施（可选）

```bash
# 必须先初始化凭证，否则 Broker 因缺少 passwd 启动失败
python scripts/mqtt_credentials.py init --device-id dev-0001
docker compose -f infra/docker-compose.yml up -d
```

Docker 将整个 `infra/mosquitto/` 目录挂载到 `/mosquitto/config` 并设为工作目录，
因此 `passwd` 生成在宿主机即可被容器读取；签发/轮换后重载见 4.2.2。

### 4.6 手机与局域网访问（重要）

先厘清三个端在各端的可见形态，避免「手机上看不到项目」的误解：

| 端 | 形态 | 手机能否看到 |
| --- | --- | --- |
| 管理后台 | Vue3 Web 页面 | 能，用手机**浏览器**打开局域网地址 |
| 设备端 | Flutter App（设备模拟器 + 移动看板） | 能，看板可查看真实数据；admin / operator 还可下发指令、确认告警，其余管理操作仍需 Web |
| 网关 | 无界面服务 | 不能，也不需要 |

也就是说：**手机上的原生 App 提供移动看板（v1.7.0 起只读，v1.9.0 起 admin / operator 可做指令与告警写操作），
设备注册、批量 OTA、用户管理等完整管理界面仍是 Web**。想在手机上演示后台，按下面步骤用局域网 IP 访问。

1. 查电脑局域网 IP（`en0` 为 Wi-Fi）：

```bash
ipconfig getifaddr en0          # 例如 192.168.1.8
```

2. 让网关监听所有网卡（默认脚本只监听 127.0.0.1，手机无法访问）：

```bash
cd gateway
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

3. 在 `gateway/.env` 放行手机来源的跨域请求（把 IP 换成上一步的结果）：

```bash
CORS_ORIGINS=http://127.0.0.1:5173,http://192.168.1.8:5173
```

4. 启动前端时把 API / WebSocket 指向电脑 IP，并监听所有网卡：

```bash
cd web
VITE_API_BASE=http://192.168.1.8:8000 \
VITE_MQTT_WS=ws://192.168.1.8:9001 \
npm run dev -- --host 0.0.0.0 --port 5173
```

> 这两项**不能写 `127.0.0.1`**：手机上的 `127.0.0.1` 指向手机自身，会表现为登录接口请求失败、看板无实时数据。
> 临时覆盖只对本次命令生效；需要长期生效可写入 `web/.env.local`。

5. 手机浏览器访问：

```
http://192.168.1.8:5173
```

端口放行清单（macOS 防火墙与路由器均需放行，且手机与电脑同一 Wi-Fi）：

| 端口 | 用途 | 谁访问 |
| --- | --- | --- |
| 5173 | Vue 管理后台 | 手机 / 电脑浏览器 |
| 8000 | 网关 REST API | 浏览器（经 5173 页面发起） |
| 9001 | MQTT over WebSocket | 浏览器实时看板 |
| 1883 | MQTT over TCP | device_app 模拟设备 |

> 手机端只看管理后台或移动看板时，只需放行 5173 / 8000 / 9001；运行 device_app 真机联调时再放行 1883。

### 4.7 移动看板（读写，写操作按角色受限）

入口：`flutter run` 后切到底部「移动看板」。首次进入显示登录页，
用管理后台同一套账号（如 `admin / admin123`）登录网关。

配置项都可在界面内修改，无需改源码：

| 配置 | 位置 | 默认值 |
| --- | --- | --- |
| 网关地址 | 登录页「网关地址」输入框，或看板右上角 `DNS` 图标 | iOS 模拟器 `http://127.0.0.1:8000`；Android 模拟器 `http://10.0.2.2:8000` |
| 账号口令 | 登录页 | 以 `gateway/.env` 为准 |
| token | 登录成功后写入系统安全存储（iOS Keychain / Android Keystore） | 重启 App 免登录 |
| 网关地址 | `shared_preferences`（非敏感配置） | 重启 App 自动回填 |
| 实时地址（ws/wss） | 看板右上角 `DNS` 图标，留空则自动推导 | `ws://<网关主机>:9001` |

真机使用：手机与电脑同一 Wi-Fi，网关以 `--host 0.0.0.0` 启动（见 4.6 第 2 步），
地址填电脑局域网 IP（如 `http://192.168.1.8:8000`），并放行 **8000 与 9001** 端口。
实时地址留空时自动由网关地址推导为 `ws://<同一主机>:9001`；Broker 改端口时可单独填写。

看板数据来源：

| 模块 | 数据来源 | 方式 |
| --- | --- | --- |
| 设备列表与在线状态 | `GET /api/devices` | REST 首屏 + 手动下拉刷新 |
| 每设备最新实时值 | `GET /api/records/latest`；`device/+/sensor/report` | REST 兜底 + 实时增量 |
| 设备上下线 | `device/+/status` | 实时订阅（含 LWT 遗嘱离线） |
| 设备分组变更 | `GET /api/devices/groups`；`device/system/group` | REST 兜底 + 实时就地更新并重拉分组概览 |
| 未处理告警 | `GET /api/alerts?status=open`；`device/system/alert` | REST 兜底 + 实时插入 |
| 单设备历史上报 | `GET /api/devices/{id}/records?limit=11&offset=N` | REST 分页（详情页，每页 10 条） |
| 指令回执 | `GET /api/devices/{id}/commands` | REST（详情页，写完自动刷新） |

搜索、筛选与分页（v1.11.0 起全部走服务端）：

| 能力 | 行为 |
| --- | --- |
| 搜索框 | 按设备名称 / ID 模糊匹配（`keyword` 请求参数），大小写不敏感；输入 300ms 防抖后重置到第 1 页 |
| 状态筛选 | 全部 / 在线 / 离线 / 有告警：分别下发 `status=online/offline`、`hasAlert=true`，切换即按新参数重拉第 1 页 |
| 分组筛选 | 用 `GET /api/devices/groups` 的分组概览渲染 chips，选择后下发 `group=<组名>` 并重置第 1 页；可与搜索、状态筛选、有告警组合；空分组名统一显示为「未分组」 |
| 列表分页 | 首屏 `page=1&pageSize=20`；设备区页脚进入视口自动请求下一页，加载中显示「加载中…」，末页显示「没有更多了」 |
| 有告警与角标 | `withAlertCount=true` 返回每设备 `open_alert_count`（数据库按 `status='open'` 聚合），筛选与卡片角标同源，不受告警列表分页大小影响 |
| 告警列表 | `status=open&page=N&pageSize=20` 按页追加；标题「未处理告警（N）」使用服务端 `total`，不再停在 50 条 |
| 汇总联动 | 汇总卡取 `/api/stats/overview` 的 `device_total` / `device_online` / `alert_open`，与分页口径解耦；并标注「筛选结果 N 台 / 全部 M 台」，筛选无结果时给出可读空状态，不清空汇总卡 |
| 实时协同 | MQTT 上报只更新已加载设备的数值 / 在线状态，不触发整页重拉；上下线事件即时改变「在线 / 离线」筛选结果；新设备或不满足当前页的设备在下次刷新（下拉、切换筛选、轮询兜底）时按服务端分页出现 |

> 性能预期：首屏只传输 1 页设备（20 条）与这些设备的最新值（`/api/records/latest?deviceIds=...`），
> 不再一次性拉取 100 台或全量最新值；滚动翻页按需追加，设备规模增长时首屏耗时基本恒定。
> 告警列表同样按页加载，避免未处理告警很多时一次性渲染。
>
> 已知边界：手动下拉刷新 / 筛选切换会重置到第 1 页（已加载的后续页不保留）；
> 实时新增设备不即时插入当前列表，需等下一次刷新；「有告警」筛选依赖网关 v1.11.0 的
> `hasAlert` / `withAlertCount` 参数，旧网关会忽略未知参数（此时请升级网关）。

设备详情页「历史上报」（v1.13.0 起分页）：

| 能力 | 行为 |
| --- | --- |
| 首屏 | 只取 1 页：`GET /api/devices/{id}/records?limit=11&offset=0`，多取 1 条仅用于判断是否还有更早数据，列表展示最新 10 条（时间倒序） |
| 加载更早 | 滚动到页脚或点击「加载更早」，按 `offset=<已取条数>` 取下一页并接在列表末尾；加载中显示「正在加载更早数据…」，取完显示「没有更早数据」，首页即空显示「暂无历史上报」 |
| 实时插入 | 该设备的新上报通过 MQTT 实时通道插到列表顶部；若用户已向下浏览，按插入行的实际高度补偿滚动偏移，正在看的历史数据不会被顶走；停留在顶部时不做补偿，新数据直接可见 |
| 兼容 | `offset` 为可选增量参数，不传时网关行为与旧版完全一致（最近 `limit` 条、时间正序）；旧网关忽略该参数时详情页只显示首屏一页 |

分组变更实时同步（v1.14.0）：

| 能力 | 行为 |
| --- | --- |
| 触发 | Web 后台在设备管理页改分组（`PATCH /api/devices/{id}`）；后台没有批量改分组入口，批量导入只在创建时写分组、不产生变更事件 |
| 事件 | 网关落库提交后向既有 MQTT 链路发布 `device/system/group`（`deviceId` / `groupName` / `previousGroupName` / `operator` / `ts`），仅在分组真正变化时发布，不新开通道 |
| 就地更新 | 看板收到事件立即更新已加载设备的 `group_name`；当前筛选有分组且该设备已不属于该组时立即移出列表，`total` 同步减一 |
| chips 与 total | 事件后 250ms 防抖重拉 `GET /api/devices/groups` 与当前分页/`/api/stats/overview`，chips、`total`、汇总卡统一以服务端口径为准（本地增量在并发改名 / 整组清空时可能失真） |
| 降级 | 实时断开期间收不到事件，但 15 秒轮询或手动刷新会用 REST 结果覆盖分组与 chips，不会「永远看不到」；新进入当前筛选的设备在下次刷新/翻页出现 |
| 兼容 | 事件 Topic 与报文为新增的可选能力：旧看板不订阅该 Topic，行为与 v1.13.x 完全一致；`PATCH` 入参、响应与错误语义不变，MQTT 不可用时编辑仍返回 200 |

告警中心（v1.15.0，HomeShell 第三个 tab）：

| 能力 | 行为 |
| --- | --- |
| 列表与筛选 | 级别（warning / critical）、状态（open / acked）、设备三组条件可组合；切换即重置到第 1 页并按新参数重拉，接口为 `GET /api/alerts?deviceId&level&status&page&pageSize`；设备条件为输入关键词远程搜索（300ms 防抖，`GET /api/devices?keyword=…&pageSize=20`），不再预拉 200 台 |
| 分页 | 首屏 `page=1&pageSize=20`，滚动到页脚自动追加下一页；加载中显示「加载中…」，末页「没有更多了」，首页即空按状态给出可读空态 |
| 每行信息 | 设备名（ID）、指标、当前值、阈值、级别、时间、状态；只读账号也能勾选查看 |
| 批量确认 | 多选后调用 `POST /api/alerts/bulk-ack`（服务端单次 `alert_ids` 上限 100 条）；超过 100 条时客户端按 100 条**串行自动分批**并合并 `acked` / `already_acked` / `not_found`；单批失败不回滚已成功批次，摘要标出失败批次并保留其选中项便于重试，不以 N 次单条请求伪装批量 |
| 实时 | 新告警经 `device/system/alert` 插入列表顶部（当前筛选不匹配时只累加角标不插入）；实时断开时 15 秒轮询第 1 页 + `/api/alerts/summary` 补齐，不漏告警 |
| tab 角标 | 角标展示本地未读计数：初始取 `/api/alerts/summary.open`，实时新增 +1，进入告警中心即清零；新告警确认后计数同步减少。告警中心在前台保持实时订阅，保证切到其它 tab 时角标仍能累加（代价是一条 MQTT 连接，与看板互不影响） |
| 权限 | 与单条确认一致：viewer 只读，批量确认按钮禁用并说明原因；operator / admin 可确认；服务端 403 提示后禁用本轮写操作，401 清除会话回登录页 |
| 兼容 | `GET /api/alerts` 原参数与默认行为不变；级别枚举收窄为 `warning` / `critical`（`info` / `notice` 返回 422）；单条 `POST /api/alerts/{id}/ack` 语义保持不变 |
| 规模化（v1.25.0） | 设备搜索在网关 `keyword` 前缀/片段匹配后按服务端分页；v1.26.0 起下拉展示已加载的全部结果（可滚动）并提供「加载更多（还有 N 台）」，不再只显示前 4 台、也不再只能用更窄的关键词；批量确认按 100 条串行分批，结果合并展示 |

写操作（仅 admin / operator，viewer 只读）：

| 操作 | 入口 | 复用的网关接口 | 行为 |
| --- | --- | --- | --- |
| 指令下发 | 设备详情「下发指令」 | `POST /api/devices/{id}/commands` | 选择类型（calibrate / config / reboot / ota）→ 填写参数 → 二次确认（目标设备 + 参数摘要）→ 提交 → 展示指令 ID / 状态 / 说明 |
| 告警确认 | 告警卡片「确认告警」 | `POST /api/alerts/{id}/ack` | 二次确认后提交，成功后立即从「未处理」列表移除 |

权限与失败处理：

- 角色只认登录会话（与 Web 同一套 JWT），移动端不复制 RBAC；viewer 的下发 / 确认按钮直接禁用并提示原因。
- 服务端返回 403 时给出可读提示，并禁用本轮写操作，不崩溃；401 清除本地 token 并回到登录页。
- 提交中按钮禁用并显示进度，防重复点击；超时 / 网络不可达 / 409（冲突）分别给出分类提示，断网不吞错。
- 参数在客户端先行校验（偏移范围、整数秒数、OTA 版本 / 地址），网关仅校验指令类型枚举。

> 风险提示：手机端可直接下发重启 / OTA 等高风险指令，且写操作会作为当前账号记入审计日志；
> 提交前请核对目标设备与参数。批量 OTA 仍建议在 Web 的「OTA 任务」页执行。

实时链路说明：

- 移动端与浏览器一样无法直连 TCP 1883，因此走 Broker 的 9001 WebSocket 端口；
- 已移除 v1.7.0 的 20 秒轮询，正常在线时由 MQTT 增量推送，首屏仍由 REST 兜底；
- 页面顶部状态条显示「实时连接中 / 已连接 / 重连中」，断线后按 3s→30s 指数退避自动重连；
- 实时不可用时自动降级为 REST 轮询（每 15 秒），状态条提示「已切换轮询」，恢复后自动停下；
- 切到设备模拟页或 App 进入后台时主动断开实时连接与轮询，回到看板后自动恢复；
- 手动下拉刷新仍会重新拉取 REST 快照，用于校正设备列表与告警列表。

异常处理：网络失败 / 超时给出地址与检查建议；401 自动清除本地 token 并回到登录页；
实时连接失败或断开只影响状态条，不影响已加载数据与设备模拟器。

> 限制：v1.9.0 已开放指令下发与告警确认，但设备注册 / 编辑 / 删除、批量 OTA 任务、用户管理
> 仍只在 Web 后台提供；token 保存在系统安全存储，账号口令不做本地记忆。

---

## 5. 功能清单

### 设备端（Flutter）

- 间歇上线 + 休眠省电状态机，UI 展示当前阶段与倒计时
- 连接即发布 retained `online`，配置 LWT 遗嘱 `offline(lwt)`
- 周期上报温度、湿度、电压、RSSI、电量、seq、时间戳
- 订阅云端指令：校准 / OTA / 重启 / 配置
- OTA 全程保持唤醒，按 10/30/55/80/100 回执进度，完成后更新固件版本
- 事件日志面板，便于现场讲解与排障
- 移动看板（v1.7.0 / v1.8.0 / v1.9.0）：登录网关后查看设备列表、实时值、单设备历史上报、未处理告警与指令回执；
  admin / operator 可下发指令（含单台 OTA）与确认告警，viewer 只读；
  MQTT over WebSocket 实时推送（9001）+ REST 首屏兜底，断线指数退避重连并显示连接状态；
  token 存 iOS Keychain / Android Keystore，兼容旧版本明文会话自动迁移

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
  导出 CSV、删除保护，详情抽屉（历史曲线 + 指令历史 + 指令下发）；
  多选后**批量指令**（config / calibrate / reboot，单次最多 200 台，逐台成功/失败/跳过与具体原因，
  `request_id` 幂等防重复下发）与**批量 OTA**跳转（v1.28.0）
- 历史查询：明细 / 按小时 / 按天聚合视图，CSV 导出
- 告警中心：统计卡片、实时告警推送、在线确认、阈值配置抽屉
- 审计日志：动作/操作人筛选，变更详情可查看
- 账号管理：新建账号、角色调整、启停、重置口令、删除保护
- OTA 管理：创建任务（多选最多 200 台，提交前预检离线/未签发凭证设备，二次确认写清失败风险）、
  任务列表、逐设备升级进度实时刷新，单台失败不拖累其他设备
- 系统状态：网关运行时长、Broker 连接与消费量、设备在线率、时序记录容量、后台任务参数（仅 admin）

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
| `device/system/group` | 网关 → 后台 | 设备分组变更（v1.14.0） |

### REST（前缀 `/api`，除登录与健康检查外均需 Bearer Token）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | 登录换取 JWT |
| GET | `/api/auth/me` | 当前账号最新信息（角色刷新） |
| GET | `/api/devices` | 设备列表（status / keyword / group / hasAlert / withAlertCount / 分页） |
| GET | `/api/devices/groups` | 分组概览（总数 / 在线 / 离线） |
| POST | `/api/devices` | 注册设备 |
| POST | `/api/devices/bulk` | 批量注册（重复跳过） |
| PATCH/DELETE | `/api/devices/{id}` | 编辑元数据 / 删除（删除仅 admin） |
| GET | `/api/devices/{id}` | 设备详情 + 最新数据 |
| GET | `/api/devices/{id}/records` | 历史数据（start / end / limit，可选 offset；不传 offset 行为不变） |
| GET | `/api/devices/{id}/records/aggregate` | 按小时/天聚合（avg/min/max） |
| GET | `/api/records/latest` | 全局最新数据（可选 status / keyword / limit / offset，不传时行为不变） |
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
| GET | `/api/system/status` | 系统状态快照（网关/Broker/存储/后台任务，仅 admin） |
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

### 10.1 管理后台代码分割（v1.13.0）

`web` 构建策略（`web/vite.config.ts`）：

- **路由全部懒加载**：仪表盘、设备、历史、告警、审计、账号、OTA、系统状态与登录页
  均由 `() => import()` 按需加载，入口只保留应用外壳、路由守卫与全局样式；
- **稳定大依赖单独分包**：`manualChunks` 将 `echarts` / `zrender` 归入 `echarts`，
  `mqtt` 归入 `mqtt`。图表库只在图表页请求，MQTT 与外壳状态灯共用且独立缓存；
- **Element Plus 保持按需引入**（`unplugin-vue-components`）：组件跟随各自视图进入异步 chunk，
  不整包合并，避免把只有部分页面使用的组件塞进入口同步包。

`npm run build`（`vue-tsc --noEmit && vite build`）产物（2026-11-04 实测，无 chunk 体积告警）：

| 产物 | 大小 | 说明 |
| --- | --- | --- |
| `index-*.js` | 312.04 kB | 入口主包（原 693.24 kB，降幅约 55%） |
| `mqtt-*.js` | 345.32 kB | MQTT 客户端，入口 `modulepreload`，与外壳状态灯同期加载 |
| `echarts-*.js` | 482.73 kB | 图表库，仅图表相关页面需要时加载 |
| `el-table-column-*.js` | 89.96 kB | Element Plus 按需组件（异步） |
| `HistoryView-*.js` | 110.64 kB | 最大的业务视图异步 chunk |
| 其余视图 chunk | 2.9 ~ 34.3 kB | 登录、仪表盘、设备、告警、审计、账号、OTA、系统状态 |

> 入口同步加载的只有 `index` + `mqtt`；`echarts` 与各业务视图均不进入首屏请求。
> 如后续新增图表页，继续沿用 `@/utils/echarts` 的按需注册，避免把全量 echarts 带入入口包。

---

## 11. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 网关报 `permission denied for table device` | init.sql 用了超级用户执行，表归属错误。用 `iot` 账号执行 init.sql，或 `ALTER TABLE ... OWNER TO iot` |
| `python3` 是 3.9.6 | macOS/Xcode 自带版本；用 `/opt/homebrew/bin/python3.14`，脚本已内置 |
| 网关 `/api/health` 显示 `mqttConnected:false` | Broker 未启动或端口不符，查看 `logs/gateway.log` 与 `logs/mosquitto.log` |
| 浏览器 / 移动看板连不上 9001 | 确认 mosquitto.conf 已配置 `protocol websockets`，并检查防火墙与手机是否同网段 |
| 设备一直离线 | 检查 LWT 报文、Topic 拼写；模拟器用 `--count 1` 时上报后会立即休眠，属正常现象 |
| 连接 Broker 返回 CONNACK 5 / 135 | 匿名已关闭：检查用户名是否等于 deviceId、口令是否为最近一次签发/轮换的值，以及 Broker 是否已重载 `passwd` |
| Web/移动端无实时数据但 REST 正常 | `/api/auth/mqtt-credentials` 未配置或返回 503；见 4.2.2 故障排查 |
| 告警只有模板文案 | 未配置 `LLM_API_KEY`，属预期降级行为 |

---

## 12. 端到端验收（真实 Broker）

`scripts/e2e/e2e_acceptance.py` 用真实 Mosquitto、MQTT over WebSocket（与移动看板
同一连接方式）、真实网关、REST 写接口与断线恢复验证真实链路；本地与 CI 均可运行。

```bash
gateway/.venv/bin/python -m pip install -r scripts/e2e/requirements.txt
gateway/.venv/bin/python scripts/e2e/e2e_acceptance.py
echo $?    # 0 = 全部 PASS；1 = 存在 FAIL
```

覆盖内容：

- 设备上线 / 上报 / 休眠下线，REST 设备列表与 `/api/records/latest` 可读；
- WebSocket 9001 秒级收到上报、上下线与 critical 告警；
- REST 下发 config 指令，模拟设备回执 success 并同步到指令历史；
- REST 确认告警，状态变为 acked（open 消失、acked 可查）；
- 告警中心：真实上报产生新告警 → `GET /api/alerts` 按 deviceId/level/status 筛选可见 →
  `POST /api/alerts/bulk-ack` 批量确认（混入不存在 ID 验证部分失败可见）→ open 不再返回、acked 可查；
- **鉴权隔离（v1.17.0）**：匿名连接 1883/9001 均被 CONNACK 拒绝；设备 A 用自己凭证
  订阅/发布设备 B 的 Topic 收不到消息；设备只用自己的 Topic 正常上报/收指令；
  Web/移动端只读身份能订阅、发布被拒；轮换设备凭证后旧凭证连接失败、新凭证可用；
- 停 Broker 后 WS 断连、REST 仍可轮询；重启 Broker 后 WS 重连、网关
  `mqtt.connected=true`，并再次收到真实上报。

隔离与清理：脚本强制自起 Broker 与网关，要求 1883/9001/8000 全部空闲，
**不再复用外部匿名 Broker**（运行前请先停掉 `start_all.sh` 托管的服务）。
每次运行在临时目录生成 gateway/web/mobile/device/probe 五套随机凭证与临时 `passwd`，
不写仓库、不提交真实口令；网关使用临时 SQLite，Broker 使用临时数据目录
（保留消息不跨运行累积），结束后停止所有托管进程、删除临时目录并等待端口释放。

本脚本已纳入 CI 的 `e2e` 作业（ubuntu-latest），由脚本退出码判定成败，
正常耗时约 2~3 分钟，超时 15 分钟。CI 失败时查看 `e2e` 作业的逐项 PASS/FAIL 与
「失败时输出日志」步骤（`mosquitto.log` / `gateway.log` / `simulator.log`）。
前置条件、逐项覆盖、常见失败原因见 [scripts/e2e/README.md](scripts/e2e/README.md)。

---

## 13. 测试与 CI

### 13.1 GitHub Actions 作业

| 作业 | 运行环境 | 内容 | 正常耗时 |
| --- | --- | --- | --- |
| `gateway` | ubuntu-latest | 安装网关依赖并跑 `pytest` | 约 1 分钟 |
| `web` | ubuntu-latest | `npm ci` + `npm run test` + `npm run build` | 约 2 分钟 |
| `device_app` | ubuntu-latest | `flutter analyze` + `flutter test` | 约 2~3 分钟 |
| `e2e` | ubuntu-latest | 安装 mosquitto，运行真实 Broker 端到端验收（37 项，含鉴权隔离、API 凭证轮换与重载结果审计） | 约 2~3 分钟 |

`e2e` 作业自包含：固定使用 1883 / 9001 / 8000，执行前检查端口占用，
每次运行由脚本在临时目录生成随机凭证与 `passwd`，**不提交任何真实口令文件**；
结束后脚本自清理并由 `if: always()` 兜底；不依赖外部服务、不写生产数据。
成败完全以脚本退出码判定，`timeout-minutes: 15` 防止卡死。
失败排查：查看作业中逐项 PASS/FAIL 与「失败时输出日志」步骤（各服务日志末尾 80 行）。

### 13.2 本地测试

| 层 | 命令 | 覆盖范围 |
| --- | --- | --- |
| 设备端单测 | `cd device_app && flutter test` | 实时报文解析与断线重连、token 迁移与安全存储、指令参数校验、REST 结果分类 |
| 设备端 Widget 测试 | `cd device_app && flutter test test/mobile_board_widget_test.dart` | 登录进入看板、指令下发二次确认 / 取消 / 回执、告警确认移除、viewer 与 403 禁用、实时降级轮询、401 回登录、分组变更移出当前筛选与 chips 刷新 |
| 告警中心 Widget 测试 | `cd device_app && flutter test test/alert_center_widget_test.dart` | 级别 / 状态筛选切换、滚动分页与没有更多、空态、批量确认成功与 403、viewer 禁用、实时入顶与未读角标清零、250 台设备远程搜索与截断提示、150 条自动分批与单批失败摘要 |
| 网关 | `cd gateway && ./.venv/bin/python -m pytest -q` | 认证、设备、告警、指令、OTA 契约；分组变更事件字段与发布降级；告警筛选 / 分页 / 批量确认（含部分失败）与 PATCH 设备 HTTP 回归 |
| Web 单元测试 | `cd web && npm run test` | 路由守卫：未登录重定向、登录回跳、admin 专属页、非 admin 拒绝、未知路径兜底 |
| Web | `cd web && npm run build` | 生产构建回归 |

Widget 测试通过 `MobileBoardPage` 的构造参数注入 fake `ApiClient` / 实时服务 / 存储，
不发起真实网络请求，也不依赖真实 Keychain / Keystore 或设备索引；每个用例至少断言一个
用户可见结果（登录页、列表项、按钮禁用状态或状态条文案）。

### 13.3 Web 单元测试

- **技术栈**：`vitest` + `jsdom`，复用生产 `vite.config.ts`（`mergeConfig`），
  因此 Vue SFC、`@` 别名与 Element Plus 按需引入的解析规则与构建完全一致；
  配置见 [web/vitest.config.ts](web/vitest.config.ts)，Node 26 实验性 `localStorage`
  的兼容补丁见 [web/test/setup.ts](web/test/setup.ts)。
- **如何跑**：

```bash
cd web
npm run test        # vitest run，CI 与本地同一命令
```

- **覆盖范围**：直接实例化生产 router（`createAppRouter`，测试注入 memory history），
  不复制守卫逻辑，断言的是导航后的真实 `currentRoute`：

| 用例 | 断言 |
| --- | --- |
| 未登录访问受保护路由 | 落到 `/login`，`redirect` 保留完整 `fullPath`（含查询参数） |
| 未登录访问根路径 | 落到 `/login`，`redirect=/dashboard` |
| 已登录访问 `/login` | 回到 `/dashboard` |
| admin 访问 `/system` | 允许进入，停留在 `/system` |
| viewer 访问 `/system` | 被拒并回到 `/dashboard` |
| operator 访问 `/users` | 被拒并回到 `/dashboard` |
| 已登录访问未知路径 | 落到兜底路由 `/dashboard` |

- **与 CI 的关系**：CI `web` 作业在 `npm ci` 后先跑 `npm run test`，再执行
  `vue-tsc --noEmit && vite build`；单测失败会直接以用例名（如
  `viewer 访问 /system → 被拒并回到 /dashboard`）标红，无需翻构建产物。
  用例不发起网络请求，不依赖网关或 Broker，可在干净环境独立运行。


---

## 14. 版本记录

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
| `v1.6.0` | 系统状态运维页与 `/api/system/status`、CI Flutter 版本对齐、手机/局域网访问文档 |
| `v1.7.0` | Flutter 只读移动看板：登录与网关地址配置、设备/实时值/历史/告警/指令回执，设备模拟器保留为默认入口 |
| `v1.8.0` | 移动看板实时化（MQTT over WebSocket 9001，替换 20 秒轮询）与凭据安全存储（Keychain/Keystore + 旧 token 迁移） |
| `v1.9.0` | 移动看板接入受限写操作：指令下发（校准/配置/重启/单台 OTA）与告警确认，复用网关既有接口并按角色 / 403 禁用 |
| `v1.9.1` | 移动看板 Widget 测试补强：登录、写操作确认/取消/回执、告警移除、权限禁用、实时降级与 401 路径 |
| `v1.9.2` | 真实 Broker 端到端验收：WebSocket 实时增量、指令回执、告警 acked、停 Broker 降级与恢复；验收固化为可重复脚本 |
| `v1.9.3` | E2E 纳入 GitHub Actions 独立作业（mosquitto + 端口检查 + 超时 + 清理）；E2E 使用临时 Broker 数据目录与 Python/Mosquitto 路径自适应 |
| `v1.10.0` | 移动看板搜索与状态筛选（防抖、有告警筛选、20 条分批懒加载、空状态）、实时事件与筛选协同；网关 `/api/records/latest` 新增可选 status / keyword / limit / offset，`/api/devices` 过滤分页补回归测试（[发布说明](docs/releases/v1.10.0.md)） |
| `v1.11.0` | 移动看板切换服务端分页与筛选，拆掉首屏 100 台与告警 pageSize=50 上限；网关 `/api/devices` 新增可选 `hasAlert` / `withAlertCount`（按设备聚合未处理告警数），`/api/records/latest` 新增可选 `deviceIds`；汇总卡改用服务端统计，实时与分页协同（[发布说明](docs/releases/v1.11.0.md)） |
| `v1.12.0` | 移动看板新增分组切换与分组筛选（与搜索、状态、有告警、服务端分页组合生效，切组重置第 1 页）；空分组统一归入「未分组」，网关分组概览同步归并（[发布说明](docs/releases/v1.12.0.md)） |
| `v1.13.0` | 移动看板设备详情「历史上报」分页（`limit+offset`，首屏一页、加载更早、三态文案、实时插入保持滚动位置）；网关 records 新增可选 `offset`（不传行为不变）；Web 路由全部懒加载 + `manualChunks` 拆分，入口主包 693.24 kB → 312.04 kB 且无体积告警（[发布说明](docs/releases/v1.13.0.md)） |
| `v1.13.1` | Web 测试基建：vitest + jsdom 复用生产 Vite 配置，7 条路由守卫用例（未登录重定向、登录回跳、admin 专属页、非 admin 拒绝、未知路径兜底）接入 CI `web` 作业（[发布说明](docs/releases/v1.13.1.md)） |
| `v1.14.0` | 设备分组变更实时化：网关在 `PATCH` 落库后发布 `device/system/group`，移动看板就地更新分组名、移出当前筛选并防抖重拉 chips / total / 汇总卡；E2E 增加真实 Broker 的分组变更验收（[发布说明](docs/releases/v1.14.0.md)） |
| `v1.15.0` | 移动端新增「告警中心」第三 tab：级别 / 状态 / 设备筛选、滚动分页、多选批量确认、实时入顶与未读角标；网关新增 `POST /api/alerts/bulk-ack`（部分失败逐类可见），告警级别校验放宽到 info/notice/warning/critical；补 `PATCH /api/devices/{id}` HTTP 回归与真实 Broker 告警中心验收（[发布说明](docs/releases/v1.15.0.md)） |
| `v1.15.1` | 交付就绪只读审计：按安全、部署、可观测性、数据生命周期、功能闭环、发布工程、文档与测试边界产出 P0/P1/P2 分级清单与执行顺序（[交付就绪清单](docs/delivery-readiness.md)） |
| `v1.16.0` | P0-2 默认凭据与登录防护：`APP_ENV=production` 启动校验默认 JWT/管理员口令/数据库并在不合规时拒绝启动；演示账号仅开发/测试默认种子；登录按 IP+账号限流（429 + `Retry-After`）；新建/重置口令最小长度与弱口令校验；登录响应结构与既有鉴权语义不变（[发布说明](docs/releases/v1.16.0.md)） |
| `v1.17.0` | P0-3 Broker 鉴权与设备凭证：1883/9001 关闭匿名并按监听加载 `password_file` + ACL；设备用户名=deviceId、只能读写自己的 Topic 子树；Web/移动端登录后换取独立只读凭证（9001 无 write）；新增设备凭证签发/轮换接口与 CLI，轮换后旧凭证失效；E2E 增加匿名拒绝、跨设备越权、客户端只读与轮换验收（[发布说明](docs/releases/v1.17.0.md)） |
| `v1.18.0` | P0-1 生产部署入口：gateway/web Dockerfile（多阶段、非 root、健康检查）、生产编排与网络隔离、nginx HTTPS 反代与安全头、生产 env 模板与一键脚本；本机真实起栈完成登录/MQTT 凭证/设备查询/模拟上报与持久化验收（[发布说明](docs/releases/v1.18.0.md)） |
| `v1.19.0` | P1-2 + P1-3 数据层：Alembic baseline 与迁移机制（既有库自动标记、不重建）、告警/审计查询索引、records/告警/审计三套保留策略与手动清理、`pg_dump` 备份/恢复脚本与隔离库恢复演练；CI 增加空库迁移与真实 PostgreSQL 作业（[发布说明](docs/releases/v1.19.0.md)） |
| `v1.20.0` | P1-1 可观测性：JSON 结构化日志与 `X-Request-Id` 全链路、`/api/health/ready` 分层就绪探针、Prometheus `/metrics`（请求/延迟/5xx、登录 429、MQTT 掉线与 DB 错误）、反代内网白名单与可直接加载的告警规则；真实停库/停 Broker 演练（[发布说明](docs/releases/v1.20.0.md)） |
| `v1.21.0` | P1-4 + P2-2 + P2-1 告警闭环：Web 多选批量确认（复用 `bulk-ack`，部分失败逐类可见、viewer 只读）、`ack_note` / `acked_by` / `acked_at` 处置元数据（迁移 `0003`，单条与批量可选备注、审计脱敏摘要）、级别口径收窄为 `warning` / `critical`；E2E 验证备注与 `acked_by`（[发布说明](docs/releases/v1.21.0.md)） |
| `v1.22.0` | P1-5 移动端发布工程：release 签名支持环境变量/`key.properties`（缺密钥显式降级并标注 `debug-signed`）、CI 在 main/标签构建 APK+AAB 并上传制品（含版本号与 SHA、`SHA256SUMS`、`SIGNING.txt`）、标签版本/构建号递增校验、Dependabot 四生态分组更新、Actions 升级到 Node 24 运行时；真实临时 keystore 验签与无密钥路径验证（[发布说明](docs/releases/v1.22.0.md)） |
| `v1.23.0` | P1-7 测试盲区收敛：OTA 全链路进 E2E（创建→下发→分片进度→成功→REST/固件/审计一致）、设备失败与超时两条失败路径；修复终态可回退、下发失败不计任务、跨设备回执、无回执永久 running（新增指令超时巡检与 `COMMAND_TIMEOUT_SECONDS`）；真实故障注入（网关重启、设备重连、DB 短暂不可用）；Postgres 作业扩到告警/设备/认证/HTTP/指令 56 条核心用例；新增真机验收清单（[发布说明](docs/releases/v1.23.0.md)） |
| `v1.24.0` | P2-4 认证生命周期：access 20 分钟 + refresh 14 天（只存指纹、单次使用与轮换、重放检测），登出/管理员吊销/改密三类吊销；Web 与移动端 401 单飞自动续期、失败回登录页；反代安全头补齐并用真实 Chrome 实测 CSP（含 ECharts blob worker 与 MQTT WSS）；修复生产构建同源 API 相对路径失效（[发布说明](docs/releases/v1.24.0.md)） |
| `v1.25.0` | P2-3 + P2-5 规模化与 CI 体验：移动端设备筛选改为关键词远程搜索（300ms 防抖、前 20 台 + 截断提示），批量确认超过 100 条自动按 100 串行分批并合并结果、单批失败保留已成功部分；CI 增加分支级 `concurrency` 取消旧运行（标签构建不取消），gateway/Flutter/E2E 失败时上传日志 artifact（保留 7 天）（[发布说明](docs/releases/v1.25.0.md)） |
| `v1.26.0` | 凭证可运维化 + 搜索体验补齐：Web/移动端设备详情展示凭证状态（绝不展示口令/完整指纹）、admin 受控轮换与吊销、二次确认写清真实影响、一次性口令（复制 + 已保存确认）、Broker 重载失败如实提示并记录审计 `result`；移动端设备搜索改为「加载更多」；E2E 增加真实 Broker 上的 API 轮换（旧凭证被拒、新凭证可用）与设备状态记录（[发布说明](docs/releases/v1.26.0.md)） |
| `v1.27.0` | 移动端概览首页：默认入口由设备模拟器改为「概览」，底部调整为 概览 / 看板 / 告警中心 / 工具（模拟器移入工具）；展示设备总数与在线率、未签发凭证、未处理告警级别分布、近 24 小时真实趋势（不足 24 小时如实提示）、异常设备（可进详情并跳看板聚焦）；实时复用告警中心单条订阅、切走/后台停监听，`CustomPainter` 绘图无新增依赖；`/api/stats/overview` 追加聚合字段，Web 与移动端同一口径（[发布说明](docs/releases/v1.27.0.md)） |
| `v1.28.0` | 设备批量操作：新增 `POST /api/devices/commands/bulk`（最多 200 台、逐台成功/失败/跳过、具体原因、`request_id` 幂等、单条审计，`ota` 走独立任务流程）；Web 设备列表跨页多选批量指令、批量 OTA 跳转、离线/未签发凭证预检、二次确认写清台数与不可逆风险；OTA 任务页展示成功/失败/待回执汇总，单台失败不拖累其他设备（[发布说明](docs/releases/v1.28.0.md)） |
