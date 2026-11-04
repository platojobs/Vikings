"""告警路由：列表与确认。"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import AlertOut, AlertSummaryOut, ApiResponse, PageOut
from app.security import ROLE_OPERATOR, require_role, require_user
from app.services import alert_service, audit_service

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("", response_model=ApiResponse[PageOut[AlertOut]])
async def list_alerts(
    deviceId: Optional[str] = Query(default=None, max_length=64),
    level: Optional[str] = Query(default=None, pattern="^(warning|critical)$"),
    status: Optional[str] = Query(default=None, pattern="^(open|acked)$"),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    rows, total = await alert_service.list_alerts(session, deviceId, level, status, page, pageSize)
    return ApiResponse(
        data=PageOut[AlertOut](
            items=[AlertOut.model_validate(row) for row in rows],
            total=total,
            page=page,
            page_size=pageSize,
        )
    )


@router.post("/{alert_id}/ack", response_model=ApiResponse[AlertOut])
async def ack_alert(
    alert_id: str,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    alert = await alert_service.ack_alert(session, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    await audit_service.log(
        session,
        operator=user.get("sub", "system"),
        action=audit_service.ACTION_ALERT_ACK,
        target_type="alert",
        target_id=alert_id,
        detail={"deviceId": alert.device_id, "level": alert.level, "metric": alert.metric},
    )
    await session.commit()
    return ApiResponse(data=AlertOut.model_validate(alert))


@router.get("/summary", response_model=ApiResponse[AlertSummaryOut])
async def alert_summary(
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    return ApiResponse(data=await alert_service.alert_summary(session))
