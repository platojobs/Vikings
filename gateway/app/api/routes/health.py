"""健康检查分层与指标端点（P1-1 / v1.20.0）。

- `/api/health`（在 stats.py）：轻量存活探针，不查库，进程活着就 200；
- `/api/health/ready`：就绪探针，真实查询数据库 + 检查 MQTT，任一失败返回 503；
- `/metrics`：Prometheus 文本指标，生产反代只允许内网来源访问。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.database import engine, probe_database
from app.metrics import render_metrics, update_db_pool_metrics

router = APIRouter(prefix="/api/health", tags=["health"])
metrics_router = APIRouter(tags=["ops"])


def _uptime_seconds(request: Request) -> int:
    started_at = getattr(request.app.state, "started_at", None)
    if started_at is None:
        return 0
    return max(0, int((datetime.now(timezone.utc) - started_at).total_seconds()))


@router.get("/ready")
async def readiness(request: Request):
    """就绪探针：数据库可查 + Broker 已连接才返回 200，否则 503 并给出明细。

    生产网关健康检查引用本端点：数据库挂了继续收流量只会把 5xx 放大，
    Broker 挂了则无法下发指令/推送告警，二者都属于「不该接流量」的状态。
    """
    database = await probe_database()
    mqtt = getattr(request.app.state, "mqtt", None)
    mqtt_connected = bool(getattr(mqtt, "is_connected", False))
    ready = bool(database["ok"]) and mqtt_connected

    data = {
        "status": "ready" if ready else "not_ready",
        "version": request.app.version,
        "uptimeSeconds": _uptime_seconds(request),
        "components": {
            "database": database,
            "mqtt": {
                "ok": mqtt_connected,
                "lastError": getattr(mqtt, "last_error", None),
                "messagesHandled": int(getattr(mqtt, "message_count", 0)),
            },
        },
    }
    body = {
        "code": 0 if ready else 503,
        "message": "ready" if ready else "not ready",
        "data": data,
    }
    return JSONResponse(
        status_code=200 if ready else 503,
        content=body,
        headers={"Cache-Control": "no-store"},
    )


@metrics_router.get("/metrics", include_in_schema=False)
async def metrics():
    """Prometheus 抓取端点；仅容器网络/内网可达，反代层另有来源限制。"""
    update_db_pool_metrics(engine)
    payload = render_metrics()
    return Response(content=payload["body"], media_type=payload["content_type"])
