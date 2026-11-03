"""统计与健康检查路由。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.rules import thresholds_snapshot
from app.schemas import ApiResponse, StatsOverviewOut
from app.security import require_user
from app.services import stats_service

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/stats/overview", response_model=ApiResponse[StatsOverviewOut])
async def overview(
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    return ApiResponse(data=await stats_service.overview(session))


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


@router.get("/rules/thresholds")
async def get_thresholds(_: dict = Depends(require_user)):
    """当前生效的告警阈值。修改阈值需调整环境变量并重启网关。"""
    return ApiResponse(data=thresholds_snapshot())
