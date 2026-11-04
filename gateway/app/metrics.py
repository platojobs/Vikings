"""Prometheus 指标定义与采集辅助（P1-1 / v1.20.0）。

指标命名统一使用 `iot_` 前缀，标签保持低基数：
- HTTP 路径使用路由模板（如 `/api/devices/{device_id}`），未匹配路由记为 `unmatched`；
- 业务标签只使用枚举值（method/status/kind），不把用户、设备 ID 写进标签。

`/metrics` 由网关自身暴露；生产反代只允许内网来源访问（见 nginx default.conf）。
"""

from typing import Any, Dict

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# ---- HTTP 请求 ------------------------------------------------------------
HTTP_REQUESTS_TOTAL = Counter(
    "iot_http_requests_total",
    "HTTP 请求总数，按方法、路由模板与状态码分类。",
    ["method", "path", "status"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "iot_http_request_duration_seconds",
    "HTTP 请求耗时（秒）。",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# ---- 登录 ----------------------------------------------------------------
LOGIN_FAILURES_TOTAL = Counter(
    "iot_login_failures_total",
    "登录失败次数（账号不存在/停用/口令错误）。",
)

LOGIN_RATE_LIMITED_TOTAL = Counter(
    "iot_login_rate_limited_total",
    "因失败次数过多被限流返回 429 的登录请求数。",
)

# ---- MQTT ----------------------------------------------------------------
MQTT_CONNECTED = Gauge(
    "iot_mqtt_connected",
    "网关与 MQTT Broker 的连接状态（1=已连接，0=断开）。",
)

MQTT_DISCONNECTS_TOTAL = Counter(
    "iot_mqtt_disconnects_total",
    "已建立连接后发生断开的次数；用于区分「从未连上」与「连上后掉线」。",
)

MQTT_CONNECT_FAILURES_TOTAL = Counter(
    "iot_mqtt_connect_failures_total",
    "MQTT 连接/重连失败次数（含首次连接失败）。",
)

MQTT_MESSAGES_TOTAL = Counter(
    "iot_mqtt_messages_total",
    "网关消费的 MQTT 消息数，按类型（report/status/ack）分类。",
    ["kind"],
)

# ---- 数据库 --------------------------------------------------------------
DB_QUERY_ERRORS_TOTAL = Counter(
    "iot_db_query_errors_total",
    "数据库语句或连接错误次数（由 SQLAlchemy handle_error 事件统计）。",
)

DB_READY = Gauge(
    "iot_db_ready",
    "最近一次 /api/health/ready 数据库探测结果（1=可用，0=不可用）。",
)

DB_READY_CHECK_DURATION_SECONDS = Gauge(
    "iot_db_ready_check_duration_seconds",
    "最近一次 /api/health/ready 数据库探测耗时（秒）。",
)

DB_POOL_CHECKED_OUT = Gauge(
    "iot_db_pool_checked_out",
    "连接池中已借出的连接数（SQLite 无连接池时为 0）。",
)

DB_POOL_CHECKED_IN = Gauge(
    "iot_db_pool_checked_in",
    "连接池中空闲的连接数（SQLite 无连接池时为 0）。",
)

DB_POOL_OVERFLOW = Gauge(
    "iot_db_pool_overflow",
    "连接池溢出连接数（超过 pool_size 后临时创建的连接）。",
)


def observe_request(method: str, path: str, status: int, duration_seconds: float) -> None:
    """记录一次 HTTP 请求计数与耗时。"""
    status_label = str(status)
    HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status=status_label).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(method=method, path=path).observe(
        duration_seconds
    )


def update_db_pool_metrics(engine: Any) -> None:
    """把 SQLAlchemy 连接池当前状态同步到 gauge。

    兼容 SQLite 的 StaticPool（没有 checkedout/checkedin 等方法）：取不到就记 0，
    不让指标采集本身成为故障点。
    """
    pool = getattr(engine, "pool", None)
    for gauge, method_name in (
        (DB_POOL_CHECKED_OUT, "checkedout"),
        (DB_POOL_CHECKED_IN, "checkedin"),
        (DB_POOL_OVERFLOW, "overflow"),
    ):
        try:
            method = getattr(pool, method_name, None)
            value = float(method()) if callable(method) else 0.0
        except Exception:
            value = 0.0
        # SQLAlchemy 的 overflow() 在连接数未超过 pool_size 时可能为负，
        # 指标语义是「溢出连接数」，负数没有意义，统一夹到 0。
        gauge.set(max(0.0, value))


def render_metrics() -> Dict[str, Any]:
    """返回 Prometheus 文本格式与 Content-Type（便于路由直接使用）。"""
    return {
        "body": generate_latest(),
        "content_type": CONTENT_TYPE_LATEST,
    }
