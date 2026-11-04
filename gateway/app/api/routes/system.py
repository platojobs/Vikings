"""系统状态路由：运维只读快照，仅管理员可见。"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import ApiResponse, SystemStatusOut
from app.security import ROLE_ADMIN, require_role
from app.services import system_service

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/status", response_model=ApiResponse[SystemStatusOut])
async def system_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_role(ROLE_ADMIN)),
):
    """返回网关 / Broker / 存储 / 后台任务的当前状态。

    仅 admin 可见：容量与连接细节属于运维信息，不对只读账号开放。
    """
    started_at = getattr(request.app.state, "started_at", None)
    if started_at is None:
        started_at = datetime.now(timezone.utc)
    uptime = max(0, int((datetime.now(timezone.utc) - started_at).total_seconds()))
    data = await system_service.collect_status(
        session,
        mqtt=request.app.state.mqtt,
        started_at=started_at,
        uptime_seconds=uptime,
        version=request.app.version,
    )
    return ApiResponse(data=data)
