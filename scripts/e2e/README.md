# 端到端验收（真实 Broker / WebSocket / 网关）

`e2e_acceptance.py` 用于验证 CI 覆盖不到的真实链路：真实 Mosquitto、真实
MQTT over WebSocket、真实网关进程、真实 REST 写接口与断线降级恢复。

脚本以「移动看板」相同的方式连接 `9001` WebSocket 订阅，逐项输出 PASS / FAIL，
全部通过时退出码为 `0`，任一项失败为 `1`，可直接用于本地发布前检查。

## 1. 前置条件

| 依赖 | 要求 | 安装 / 检查 |
| --- | --- | --- |
| Mosquitto | 2.x，含 WebSocket 支持 | `brew install mosquitto`；`mosquitto -h` |
| Broker 配置 | `infra/mosquitto/mosquitto.conf` 同时含 `listener 1883` 与 `listener 9001` + `protocol websockets` | 脚本会自动校验 |
| 网关虚拟环境 | `gateway/.venv` 可用，已装网关依赖 | `cd gateway && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt` |
| E2E 额外依赖 | `paho-mqtt` / `websocket-client` / `httpx` | `gateway/.venv/bin/python -m pip install -r scripts/e2e/requirements.txt` |
| 端口 | `1883`、`9001`、`8000` 空闲（脚本会自起进程并托管） | `lsof -nP -iTCP:1883 -iTCP:9001 -iTCP:8000 -sTCP:LISTEN` |

`5173` 仅用于环境核对时报告占用情况，E2E 本身不依赖 Web 前端。

## 2. 执行方式

```bash
cd /path/to/Vikings
gateway/.venv/bin/python scripts/e2e/e2e_acceptance.py
echo $?    # 0 = 全部 PASS，1 = 存在 FAIL
```

可选参数：`--username` / `--password`（默认 `admin / admin123`，用于临时库初始化）。

日志落在 `.run/e2e/`（已被 `.gitignore` 忽略）：`mosquitto.log`、`gateway.log`、
`simulator.log`，失败时优先查看这里。

## 3. 端口与进程策略

| 端口 | 用途 | 脚本行为 |
| --- | --- | --- |
| 1883 | 设备 MQTT over TCP | 空闲则由脚本启动 Broker；已占用则复用现有 Broker |
| 9001 | 看板 MQTT over WebSocket | 同上，1883 / 9001 必须同时空闲或同时被占用 |
| 8000 | 网关 REST | 空闲则由脚本用**临时 SQLite** 启动；已占用则复用现有网关 |
| 5173 | Web 前端 | 仅检查占用，不启动、不停止 |

隔离原则：

- 脚本启动的网关通过 `DATABASE_URL=sqlite+aiosqlite:///<临时文件>` 覆盖 `.env`，
  不会写入生产 PostgreSQL；临时库在结束时删除。
- 脚本只终止自己启动的进程（`start_new_session=True`），不杀外部已运行的服务。
- 结束时关闭 WS 客户端、模拟器、网关、Broker，并等待端口释放。

> 降级验证需要脚本托管 Broker。若 1883/9001 已被外部 Broker 占用，脚本会复用，
> 但 `degrade_disconnect` 会 FAIL 并提示「Broker 由外部托管」——这是有意的保护，
> 验收前请先停掉外部 Broker，让脚本自起。

## 4. 覆盖项（21 项）

| 步骤 | 验证内容 |
| --- | --- |
| `env_preflight` | mosquitto / venv / 依赖 / 配置 / 端口状态 |
| `broker` | 启动或复用 1883 + 9001 WebSocket Broker |
| `gateway` | 启动或复用网关，临时 SQLite，不碰生产库 |
| `auth_login` | admin 登录，角色校验 |
| `ws_connected` | paho 以 `transport=websockets` 连接 9001 并订阅四个 Topic |
| `simulator` | 启动模拟设备（`voltage=2.90` 触发 critical） |
| `realtime_report` | WS 收到上报，端到端延迟 < 3000ms |
| `rest_device` | REST 设备列表与 `/api/records/latest` 可读，状态 online |
| `realtime_status` | WS 收到 online 与 offline（含 LWT / 休眠下线） |
| `realtime_alert` | WS 秒级收到 `device/system/alert`，含 `alertId` |
| `command_window` | 等设备下一轮唤醒窗口（指令只能在窗口内收到） |
| `command_dispatch` | REST `POST /commands` 下发 config，返回 `cmd_id` |
| `command_ack` | WS 收到设备 ack success，REST 指令历史同步 success |
| `alert_ack` | REST 确认告警，状态 acked，open 不再返回、acked 可查 |
| `stop_simulator` | 停止模拟器 |
| `degrade_disconnect` | 停 Broker，WS 客户端触发 `on_disconnect`（轮询降级前提） |
| `degrade_polling_rest` | Broker 停机期间 REST 仍可用（看板可切轮询） |
| `recover_broker` | 重启 Broker |
| `recover_ws` | WS 自动重连，网关 `/api/system/status` 的 `mqtt.connected=true` |
| `recover_real_data` | 恢复后 TCP 发布新上报，WS 能再次收到（链路真正恢复） |
| `cleanup` | 进程停止、端口释放、临时库删除 |

## 5. 常见失败原因

| 现象 | 原因与处理 |
| --- | --- |
| `未安装 mosquitto` / `缺少 Broker 配置` | 执行 `brew install mosquitto`；确认配置文件含 9001 + `protocol websockets` |
| `gateway 虚拟环境缺少 E2E 依赖` | 安装 `scripts/e2e/requirements.txt` |
| `1883/9001 仅有一个被占用` | 有残留进程；先 `lsof -nP -iTCP:1883 -iTCP:9001` 并停止 |
| `Broker 启动失败` | 看 `.run/e2e/mosquitto.log`；常见为配置路径或端口冲突 |
| `网关 25s 内未就绪` | 看 `.run/e2e/gateway.log`；常见为依赖缺失或临时库不可写 |
| `WebSocket 9001 未完成 MQTT 握手` | Broker 未启用 websockets / 被防火墙拦截 / 9001 未监听 |
| `未收到上报 / 告警` | 模拟器未连上 1883；检查 `.run/e2e/simulator.log` 与 Topic 拼写 |
| `设备 ack 超时` | 指令不在设备唤醒窗口内；脚本已等待下一轮上报，若仍失败说明设备未订阅 `cmd` |
| `Broker 恢复后 WS 未重连` | 客户端自动重连上限 25s；检查 9001 是否真的重新监听 |
| `网关 MQTT 未恢复` | 网关指数退避最长 30s；脚本等待 40s，超时看 gateway.log |
| `degrade_disconnect` 提示外部托管 | 先停止外部 Broker，再让脚本托管 |
| `端口清理异常` | 有子进程未退出；脚本已 terminate → kill，仍失败请手动 `lsof` 检查 |

## 6. 与 CI 的关系

GitHub Actions 的 CI 只跑 `flutter analyze/test`、网关 `pytest` 与 `web build`，
不安装 Mosquitto、不启动真实 Broker，因此**无法覆盖**本脚本验证的链路：

- MQTT over WebSocket 9001 的真实握手与实时增量；
- 设备模拟器的唤醒窗口、指令 ack 与状态机；
- 告警从 MQTT 到 REST 的 acked 状态；
- 停 Broker 后的客户端断连、REST 降级与恢复重连。

因此本脚本定位为**发布前本地验收资产**，不纳入 CI；CI 保持快速、无外部依赖。
每次涉及 Broker / 实时链路 / 指令 / 告警的改动，发布前应至少跑一次本脚本。
