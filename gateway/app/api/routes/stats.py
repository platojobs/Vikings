"""统计与健康检查路由。"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import ApiResponse, BatteryHealthOut, StatsOverviewOut
from app.security import require_user
from app.services import battery_service, stats_service

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/stats/overview", response_model=ApiResponse[StatsOverviewOut])
async def overview(
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    return ApiResponse(data=await stats_service.overview(session))


@router.get("/stats/battery-health", response_model=ApiResponse[BatteryHealthOut])
async def battery_health(
    group: Optional[str] = Query(default=None, max_length=64, description="设备分组"),
    days: int = Query(default=7, ge=1, le=30, description="分析窗口天数（含今天）"),
    limit: int = Query(default=20, ge=1, le=100, description="风险清单最多返回台数"),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    """电池健康与续航看板（v1.32.0）。

    只读端点，viewer / operator / admin 均可访问；返回的是窗口内真实上报的聚合，
    窗口内无记录的设备计为 `unknown`，不补假值。
    """
    return ApiResponse(
        data=await battery_service.battery_health(session, group=group, days=days, limit=limit)
    )


@router.get("/health")
async def health(request: Request):
    """存活探针：同时暴露 Broker 连接状态，便于运维快速定位问题在网关还是 MQTT。"""
    mqtt = request.app.state.mqtt
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "status": "up",
            "mqttConnected": mqtt.is_connected,
            "mqttLastError": mqtt.last_error,
            "messagesHandled": mqtt.message_count,
        },
    }
