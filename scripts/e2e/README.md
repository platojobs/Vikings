# 端到端验收（真实 Broker / WebSocket / 网关）

`e2e_acceptance.py` 用真实 Mosquitto、真实 MQTT over WebSocket、真实网关进程、
真实 REST 写接口与断线恢复来验证真实链路。它既可在本地发布前运行，也已纳入
GitHub Actions 的 `e2e` 作业（见第 6 节）。

脚本以「移动看板」相同的方式连接 `9001` WebSocket 订阅，逐项输出 PASS / FAIL，
全部通过时退出码为 `0`，任一项失败为 `1`。

## 1. 前置条件

| 依赖 | 要求 | 安装 / 检查 |
| --- | --- | --- |
| Mosquitto | 2.x，含 WebSocket 支持 | `brew install mosquitto`；`mosquitto -h` |
| Broker 配置 | `infra/mosquitto/mosquitto.conf` 同时含 `listener 1883` 与 `listener 9001` + `protocol websockets`，且 `per_listener_settings true`、两个监听均 `allow_anonymous false` | 脚本会自动校验 |
| 网关运行环境 | 本地优先 `gateway/.venv`；无 venv 时自动退回当前 Python（需已装网关依赖） | `cd gateway && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt` |
| E2E 额外依赖 | `paho-mqtt` / `websocket-client` / `httpx` | `gateway/.venv/bin/python -m pip install -r scripts/e2e/requirements.txt` |
| 端口 | `1883`、`9001`、`8000` **必须全部空闲**（脚本自起 Broker 与网关并托管，不接受复用） | `lsof -nP -iTCP:1883 -iTCP:9001 -iTCP:8000 -sTCP:LISTEN` |
| 凭证 | 无需预置；脚本每次运行在临时目录随机生成 gateway / web / mobile / 设备 / probe 凭证与 `passwd` | 自动完成，不读写仓库内 `infra/mosquitto/passwd` |

`5173` 仅用于环境核对时报告占用情况，E2E 本身不依赖 Web 前端。

## 2. 执行方式

```bash
cd /path/to/Vikings
gateway/.venv/bin/python scripts/e2e/e2e_acceptance.py
echo $?    # 0 = 全部 PASS，1 = 存在 FAIL
```

CI 中无需 venv，安装依赖后直接运行：

```bash
pip install -r gateway/requirements.txt -r scripts/e2e/requirements.txt
python scripts/e2e/e2e_acceptance.py
```

可选参数：`--username` / `--password`（默认 `admin / admin123`，用于临时库初始化）。

日志落在 `.run/e2e/`（已被 `.gitignore` 忽略）：`mosquitto.log`、`gateway.log`、
`simulator.log`，失败时优先查看这里。

## 3. 端口与进程策略

| 端口 | 用途 | 脚本行为 |
| --- | --- | --- |
| 1883 | 设备 MQTT over TCP | 必须空闲；脚本启动 Broker，已占用直接报错（不复用） |
| 9001 | 看板 MQTT over WebSocket | 必须空闲；与 1883 一并由脚本启动 |
| 8000 | 网关 REST | 必须空闲；脚本用**临时 SQLite** 与临时凭证启动，已占用直接报错 |
| 5173 | Web 前端 | 仅检查占用，不启动、不停止 |

隔离原则：

- 每次运行把 `infra/mosquitto/mosquitto.conf`、`acl.device`、`acl.client` 复制到临时目录，
  生成 gateway / web / mobile / device / probe 五套随机口令与临时 `passwd`；
  **不复用外部匿名 Broker**，也不读写仓库内的真实凭证。
- 脚本启动的网关通过 `DATABASE_URL=sqlite+aiosqlite:///<临时文件>` 覆盖 `.env`，
  不会写入生产 PostgreSQL；临时库在结束时删除。
- Broker 的 `persistence_location` 是相对路径，脚本把其工作目录指向临时目录，
  保留消息不会在多次运行间累积，也不会写入仓库内 `infra/mosquitto/data/`。
- 脚本只终止自己启动的进程（`start_new_session=True`），不杀外部已运行的服务。
- 结束时关闭 WS 客户端、模拟器、网关、Broker，删除临时 SQLite 与 Broker 数据目录，
  并等待端口释放。

> 鉴权验收依赖「Broker、网关、凭证」三者来自同一次运行。复用外部 Broker 或网关
> 会导致口令不一致，因此脚本选择直接失败。验收前请先停掉 `start_all.sh` 或本机
> Mosquitto / uvicorn。

## 4. 覆盖项（当前 41 项，核心步骤如下）

| 步骤 | 验证内容 |
| --- | --- |
| `env_preflight` | mosquitto / venv / 依赖 / 鉴权配置 / 端口状态 |
| `mqtt_credentials` | 复制配置与 ACL，生成 gateway / web / mobile / device / probe 随机凭证与临时 `passwd` |
| `broker` | 独占 1883 + 9001 WebSocket Broker，加载临时凭证与 ACL |
| `gateway` | 独占 8000 启动网关，注入临时 Broker 凭证，临时 SQLite，不碰生产库 |
| `auth_login` | admin 登录，角色校验 |
| `ws_connected` | paho 以 `web-console` 凭证、`transport=websockets` 连接 9001 并订阅固定 Topic 列表 |
| `mqtt_auth_isolation` | 匿名 1883/9001 被拒；设备 A 订阅/发布设备 B Topic 收不到；设备自 Topic 正常；web/mobile 只读；轮换后旧凭证失败、新凭证可用 |
| `simulator` | 启动模拟设备（`voltage=2.90` 触发 critical） |
| `realtime_report` | WS 收到上报，端到端延迟 < 3000ms |
| `rest_device` | REST 设备列表与 `/api/records/latest` 可读，状态 online |
| `shadow_history_rollback` | 真实 REST 验证 v0 基线、v1/v2 快照、回滚生成 v3、旧快照与 reported 不变、重复回滚幂等、历史分页，以及设备 reported `null` 清键后恢复 synced |
| `realtime_status` | WS 收到 online 与 offline（含 LWT / 休眠下线） |
| `realtime_alert` | WS 秒级收到 `device/system/alert`，含 `alertId` |
| `command_window` | 等设备下一轮唤醒窗口（指令只能在窗口内收到） |
| `command_dispatch` | REST `POST /commands` 下发 config，返回 `cmd_id` |
| `command_ack` | WS 收到设备 ack success，REST 指令历史同步 success |
| `alert_ack` | REST 确认告警，状态 acked，open 不再返回、acked 可查 |
| `alert_center_bulk_ack` | 上报产生新告警 → REST 按 deviceId/level/status 筛选可见 → 批量确认（含不存在 ID）→ open 不再返回、acked 可查 |
| `group_change` | REST 改分组 → WS 收到 `device/system/group` → REST 详情与分组概览一致 |
| `stop_simulator` | 停止模拟器 |
| `degrade_disconnect` | 停 Broker，WS 客户端触发 `on_disconnect`（轮询降级前提） |
| `degrade_polling_rest` | Broker 停机期间 REST 仍可用（看板可切轮询） |
| `recover_broker` | 重启 Broker |
| `recover_ws` | WS 自动重连，网关 `/api/system/status` 的 `mqtt.connected=true` |
| `recover_real_data` | 恢复后 TCP 发布新上报，WS 能再次收到（链路真正恢复） |
| `cleanup` | 进程停止、端口释放、临时库与临时 Broker 数据目录删除 |

## 5. 常见失败原因

| 现象 | 原因与处理 |
| --- | --- |
| `未安装 mosquitto` / `缺少 Broker 配置` | macOS `brew install mosquitto`；Ubuntu `sudo apt-get install -y mosquitto`；确认配置含 9001 + `protocol websockets` |
| `缺少网关运行依赖` | 同时安装 `gateway/requirements.txt` 与 `scripts/e2e/requirements.txt` |
| CI 上 1883 被占用 | apt 安装的 `mosquitto.service` 自动启动；作业已 stop/disable/pkill，仍占用则检查端口步骤会直接报错 |
| `1883/9001 已被占用` | E2E 要求独占 Broker；先停 `start_all.sh` / 本机 Mosquitto |
| `8000 已被占用` | E2E 要求自起网关以匹配临时凭证；先停 `start_all.sh` / 本机 uvicorn |
| `Broker 启动失败` | 看 `.run/e2e/mosquitto.log`；常见为 `passwd` 路径、端口冲突或 ACL 文件缺失 |
| 网关 `mqttConnected:false` | 临时 `MQTT_PASSWORD` 与 `passwd` 不一致，或 Broker 未加载本次临时配置 |
| 匿名连接居然成功 | 仓库配置出现 `allow_anonymous true` 或缺少 `per_listener_settings true`；`env_preflight` 会直接拦截 |
| `网关 25s 内未就绪` | 看 `.run/e2e/gateway.log`；常见为依赖缺失或临时库不可写 |
| `WebSocket 9001 未完成 MQTT 握手` | Broker 未启用 websockets / 被防火墙拦截 / 9001 未监听 |
| `未收到上报 / 告警` | 模拟器未连上 1883；检查 `.run/e2e/simulator.log` 与 Topic 拼写 |
| `设备 ack 超时` | 指令不在设备唤醒窗口内；脚本已等待下一轮上报，若仍失败说明设备未订阅 `cmd` |
| `Broker 恢复后 WS 未重连` | 客户端自动重连上限 25s；检查 9001 是否真的重新监听 |
| `网关 MQTT 未恢复` | 网关指数退避最长 30s；脚本等待 40s，超时看 gateway.log |
| `degrade_disconnect` 提示外部托管 | 先停止外部 Broker，再让脚本托管 |
| `端口清理异常` | 有子进程未退出；脚本已 terminate → kill，仍失败请手动 `lsof` 检查 |

## 6. 与 CI 的关系（新增 `e2e` 作业）

`.github/workflows/ci.yml` 中的 `e2e` 作业固定在已验证的 `ubuntu-24.04` 上自包含运行本脚本，
避免 `ubuntu-latest` 迁移导致 Broker 运行环境漂移：

| 项目 | 内容 |
| --- | --- |
| 触发 | 与其它作业相同：`push` / `pull_request` 到 `main` |
| 依赖 | `apt-get install mosquitto`；`pip install gateway/requirements.txt + scripts/e2e/requirements.txt` |
| 凭证 | 脚本运行时在临时目录生成随机口令与 `passwd`，workflow **不提交任何真实口令文件** |
| 端口 | 固定 `1883` / `9001` / `8000`，执行前检查占用，避免与 runner 残留服务冲突 |
| 耗时 | 正常约 2~3 分钟（apt + pip 安装占大部分，验收本身约 13 秒） |
| 超时 | `timeout-minutes: 15`，防止卡死占用 runner |
| 成败 | 完全以脚本退出码判定：`0` 全 PASS，`1` 存在 FAIL |
| 清理 | 脚本自行清理；作业另有 `if: always()` 兜底 `pkill`，并输出 `.run/e2e/*.log` 便于排查 |

CI 覆盖：鉴权隔离（匿名拒绝、跨设备越权、客户端只读、凭证轮换）、
MQTT over WebSocket 9001 握手与实时增量、设备唤醒窗口与指令 ack、影子历史/精确回滚与
reported `null` 清键、告警从 MQTT 到 REST 的 acked 状态、停 Broker 后的断连/REST 降级/恢复重连。

排查方式：打开 `e2e` 作业 → 查看「运行真实 Broker 端到端验收」的逐项 PASS/FAIL →
查看「失败时输出日志」步骤中 `mosquitto.log`、`gateway.log`、`simulator.log` 的末尾 80 行。
常见原因见第 5 节；若 runner 的 apt mosquitto 不含 WebSocket 支持，会在
`ws_connected` / `broker` 步骤失败并明确提示。
