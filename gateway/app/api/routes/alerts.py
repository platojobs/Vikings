"""告警路由：列表与确认。"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import (
    AlertBulkAckRequest,
    AlertBulkAckResult,
    AlertOut,
    AlertSummaryOut,
    ApiResponse,
    PageOut,
)
from app.security import ROLE_OPERATOR, require_role, require_user
from app.services import alert_service, audit_service

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

# 既有规则只产生 warning / critical；这里向前兼容 info / notice，
# 既保证老数据可筛选，也不对历史枚举做破坏性收紧。
ALERT_LEVEL_PATTERN = "^(info|notice|warning|critical)$"


@router.get("", response_model=ApiResponse[PageOut[AlertOut]])
async def list_alerts(
    deviceId: Optional[str] = Query(default=None, max_length=64),
    level: Optional[str] = Query(default=None, pattern=ALERT_LEVEL_PATTERN),
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


@router.post("/bulk-ack", response_model=ApiResponse[AlertBulkAckResult])
async def bulk_ack_alerts(
    body: AlertBulkAckRequest,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    """批量确认：一次请求完成多条，部分失败按类别返回，不整批 404。

    权限与单条确认一致（operator / admin）；viewer 由 require_role 直接 403。
    事务与审计与单条确认共用，避免 N 次请求与 N 条审计噪音。
    """
    result = await alert_service.bulk_ack_alerts(session, body.alert_ids)
    await audit_service.log(
        session,
        operator=user.get("sub", "system"),
        action=audit_service.ACTION_ALERT_ACK,
        target_type="alert",
        target_id=",".join(result["acked"][:5]),
        detail={
            "bulk": True,
            "requested": result["requested"],
            "acked": len(result["acked"]),
            "already_acked": len(result["already_acked"]),
            "not_found": len(result["not_found"]),
        },
    )
    await session.commit()
    return ApiResponse(data=AlertBulkAckResult(**result))


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
