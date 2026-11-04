# 可观测性（v1.20.0 / P1-1）

目标：**网关、数据库、Broker 任一层出问题，运维在两分钟内能从指标和日志判断出是哪一层**。

## 1. 两分钟定位流程

1. `GET https://<host>/api/health`：200 说明网关进程还活着（**不查库、不查 Broker**）。
2. `GET https://<host>/api/health/ready`：
   - 200 → 数据库可查询、Broker 已连接；
   - 503 → 读 `data.components.database` / `data.components.mqtt`，直接看出是哪一层；
   - 响应头 `X-Request-Id` 与 body 的 `requestId` 可用于后续日志检索。
3. `GET https://<host>/metrics`（内网）：
   - `iot_mqtt_connected`、`iot_mqtt_disconnects_total` → Broker 层；
   - `iot_db_ready`、`iot_db_query_errors_total`、`iot_db_pool_checked_out` → 数据库层；
   - `iot_http_requests_total{status=~"5.."}`、`iot_http_request_duration_seconds` → 网关/业务层；
   - `iot_login_failures_total`、`iot_login_rate_limited_total` → 登录/安全层。
4. 用 requestId 搜网关日志（见 2.3），确认异常堆栈与具体路由。

## 2. 日志

### 2.1 格式切换

| 环境变量 | 取值 | 用途 |
| --- | --- | --- |
| `LOG_FORMAT` | `text`（默认） / `json` | 本地人类可读；生产结构化 |
| `LOG_LEVEL` | `INFO`（默认） | 日志级别 |

生产 `docker-compose.prod.yml` 默认 `LOG_FORMAT=json`。uvicorn 自带 access log 已关闭，避免与统一请求日志重复且缺少 requestId。

### 2.2 字段说明

每条请求日志为单行 JSON：

```json
{"time":"2026-11-05T12:34:56.789Z","level":"INFO","logger":"app.request","message":"GET /api/devices -> 200 12.3ms","requestId":"trace-abc-123","event":"http_request","method":"GET","path":"/api/devices","status":200,"durationMs":12.3,"clientIp":"10.0.0.8","user":"admin","error":null}
```

| 字段 | 含义 |
| --- | --- |
| `time` | UTC ISO8601 |
| `level` | 日志级别 |
| `logger` | 模块名；请求日志固定 `app.request` |
| `message` | 人类可读摘要 |
| `requestId` | 请求追踪 ID（见 2.3） |
| `event` | `http_request` 表示访问日志 |
| `method` / `path` | HTTP 方法与实际路径 |
| `status` | 响应状态码 |
| `durationMs` | 服务端处理耗时（毫秒） |
| `clientIp` | 客户端 IP（经反代时由 `X-Forwarded-For` 还原） |
| `user` | Bearer Token 中的用户名；无 token 为 `-`，无效 token 为 `anonymous` |
| `error` | 异常摘要（仅失败/异常时出现，已脱敏） |

**不记录**请求体、请求头、查询串与 Cookie。`Authorization`、`Bearer` token、`password/token/secret` 键值、数据库 URL 中的口令全部由 `redact_text()` 替换为 `[REDACTED]`，异常堆栈同样经过脱敏。

### 2.3 请求 ID 追踪

- 反代透传上游 `X-Request-Id`；没有则网关生成 32 位十六进制 UUID；
- 响应头始终回写 `X-Request-Id`；错误响应体额外带 `requestId`；
- 日志里所有记录自动带 `requestId`（ContextVar 贯穿整个请求）。

用户报错时，让其提供响应体里的 `requestId`，然后：

```bash
# 生产（JSON 日志）
docker compose -f infra/docker-compose.prod.yml logs gateway --since 30m | \
  jq -c 'select(.requestId=="<requestId>")'

# 本地
grep '<requestId>' logs/gateway.log
```

反代访问日志也记录 `rid=`（上游传入）与 `grid=`（网关回写），可对齐 nginx 与网关。

### 2.4 常见错误定位

| 现象 | 先看 | 判断 |
| --- | --- | --- |
| 所有接口慢 | `iot_http_request_duration_seconds` 的 p95/p99 与 `iot_db_ready_check_duration_seconds` | 只有 ready 查询慢 → 数据库；所有路由同步变慢 → 数据库/连接池 |
| 间歇 500 | 按 requestId 搜 `app.request` 的 `error` + `iot_db_query_errors_total` | 有 DB 错误 → 数据库；无 DB 错误 → 业务异常 |
| 设备数据不进库 | `iot_mqtt_connected`、`iot_mqtt_messages_total{kind="report"}` | 连接 0 → Broker/凭证；连接 1 但消息 0 → ACL/Topic |
| 后台登录失败 | `iot_login_failures_total`、`iot_login_rate_limited_total` | 失败上升但 429 不升 → 口令/账号；429 上升 → 限流/攻击 |
| 告警推送不出去 | `/api/health/ready` 的 `components.mqtt` | `ok=false` → Broker 或网关连接 |

## 3. 健康检查分层

| 端点 | 语义 | 查库 | 查 Broker | 典型使用方 |
| --- | --- | --- | --- | --- |
| `GET /api/health` | 存活（liveness） | 否 | 否 | 进程级探针、E2E、现有调用方 |
| `GET /api/health/ready` | 就绪（readiness） | 真实 `SELECT 1`（3 秒超时） | 读取连接状态 | 容器 `HEALTHCHECK`、反代启动依赖、人工排查 |

`/api/health` 返回结构保持不变（`status` / `mqttConnected` / `mqttLastError` / `messagesHandled`），只是不再作为容器健康判据。

**生产为什么用 `/ready`**：数据库不可用时继续接流量只会把 5xx 放大；Broker 掉线时无法下发指令、也无法推送告警。两者都属于「不该被标记为健康」。因此 `gateway/Dockerfile` 与 `docker-compose.prod.yml` 的 `healthcheck` 都请求 `/api/health/ready`。

`/ready` 返回示例（未就绪）：

```json
{
  "code": 503,
  "message": "not ready",
  "data": {
    "status": "not_ready",
    "version": "1.21.0",
    "uptimeSeconds": 123,
    "components": {
      "database": {"ok": false, "backend": "postgresql", "latencyMs": 24.5, "error": "TimeoutError: ..."},
      "mqtt": {"ok": true, "lastError": null, "messagesHandled": 0}
    }
  }
}
```

## 4. 指标清单

| 指标 | 类型 | 标签 | 说明 |
| --- | --- | --- | --- |
| `iot_http_requests_total` | Counter | `method`,`path`,`status` | 请求数，按路由模板与状态码分类 |
| `iot_http_request_duration_seconds` | Histogram | `method`,`path` | 请求耗时，桶 5ms~10s |
| `iot_login_failures_total` | Counter | - | 登录失败（账号不存在/停用/口令错） |
| `iot_login_rate_limited_total` | Counter | - | 登录限流 429 次数 |
| `iot_mqtt_connected` | Gauge | - | 1=已连接，0=断开 |
| `iot_mqtt_disconnects_total` | Counter | - | 已连接后掉线次数 |
| `iot_mqtt_connect_failures_total` | Counter | - | 连接/重连失败次数 |
| `iot_mqtt_messages_total` | Counter | `kind` | 消费消息数（report/status/ack） |
| `iot_db_query_errors_total` | Counter | - | SQLAlchemy `handle_error` 统计的语句/连接错误 |
| `iot_db_ready` | Gauge | - | 最近一次 ready 探测结果 |
| `iot_db_ready_check_duration_seconds` | Gauge | - | 最近一次 ready 探测耗时 |
| `iot_db_pool_checked_out` / `_in` / `_overflow` | Gauge | - | 连接池状态（SQLite 记 0） |
| `iot_backup_last_success_timestamp_seconds` | Gauge | - | 备份脚本写入（node_exporter textfile） |
| `iot_backup_last_status` | Gauge | - | 1=最近备份成功，0=失败 |

路径标签使用 FastAPI 路由模板（如 `/api/devices/{device_id}`），未匹配路由记 `unmatched`，避免设备 ID 造成基数爆炸。

### 4.1 采集与访问控制

网关 8000 端口只在容器网络内，不发布到宿主机；`/metrics` 经反代转发，且只允许
`127.0.0.1`、`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16` 访问，公网返回 403。
Prometheus 与网关同机部署时可直接抓 `http://gateway:8000/metrics`（加入 `iot-prod_backend` 网络）或抓内网反代地址。

```yaml
# prometheus.yml 片段
scrape_configs:
  - job_name: iot-gateway
    metrics_path: /metrics
    static_configs:
      - targets: ["gateway:8000"]
  - job_name: iot-gateway-ready
    metrics_path: /probe
    params:
      module: [http_2xx]
    static_configs:
      - targets: ["https://iot.example.com/api/health/ready"]
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox-exporter:9115
```

## 5. 告警口径与阈值

规则文件：[infra/prometheus/iot-alerts.yml](../infra/prometheus/iot-alerts.yml)，可直接 `rule_files` 加载。

| 告警 | 指标 / 条件 | 阈值 | 建议动作 |
| --- | --- | --- | --- |
| `IotGatewayDown` | `up{job="iot-gateway"} == 0` | 持续 1m | 看容器状态与网关日志 |
| `IotReadinessFailing` | `probe_success{job="iot-gateway-ready"} == 0` | 持续 1m | 打开 `/ready` 看 components |
| `IotDbNotReady` | `iot_db_ready == 0` | 持续 2m | 查 PostgreSQL 与连接池 |
| `IotMqttDisconnected` | `iot_mqtt_connected == 0` | 持续 2m | 查 Broker 容器/端口/凭证 |
| `IotMqttDisconnectSpike` | `increase(iot_mqtt_disconnects_total[10m]) > 3` | 10m 内 3 次 | 查 Broker 重启/网络抖动 |
| `IotHttp5xxRatioHigh` | 5xx / total | > 5% 持续 5m | 按 requestId 查异常 |
| `IotHttp5xxSpike` | 5xx rate | > 1/s 持续 5m | 低流量场景更敏感 |
| `IotLoginRateLimitedSpike` | `increase(iot_login_rate_limited_total[10m]) > 10` | 10m 内 10 次 | 查来源 IP，考虑反代限流 |
| `IotDbQueryErrors` | `rate(iot_db_query_errors_total[5m]) > 0.1` | 每分钟 6 次 | 查 DB 日志与慢查询 |
| `IotBackupFailed` | `iot_backup_last_status == 0` | 5m | 立即手工重跑备份 |
| `IotBackupStale` | `time() - last_success > 129600` | 36 小时 | 查 cron/磁盘/权限 |

备份指标写入方式（cron 中设置 `BACKUP_METRICS_FILE` 指向 node_exporter textfile 目录）：

```bash
0 2 * * * cd /opt/vikings && \
  BACKUP_METRICS_FILE=/var/lib/node_exporter/textfile/iot-backup.prom \
  ./scripts/pg_backup.sh --keep 30 >> logs/backup.log 2>&1
```

## 6. 本地验证

```bash
# JSON 日志 + 真实请求 ID
LOG_FORMAT=json .venv/bin/python -m uvicorn app.main:app --port 8000
curl -H 'X-Request-Id: trace-demo' -D - http://127.0.0.1:8000/api/health

# 就绪与指标
curl -i http://127.0.0.1:8000/api/health/ready
curl -s http://127.0.0.1:8000/metrics | grep iot_
```

更完整的真实故障演练（停库、停 Broker、制造 500、脱敏检查）见
[docs/releases/v1.20.0.md](releases/v1.20.0.md)。
