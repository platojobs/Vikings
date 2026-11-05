"""告警通知配置与投递记录（v1.29.0）。

安全约定：
- GET 只返回「是否已配置」，不回显 URL / 签名密钥 / SMTP 口令；
- PATCH 审计只记录变更字段名，不记录值；
- 测试通知真实调用通道，结果逐通道返回。
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import (
    ApiResponse,
    NotificationConfigOut,
    NotificationConfigUpdate,
    NotificationDeliveryOut,
    NotificationTestRequest,
    NotificationTestResult,
)
from app.security import ROLE_ADMIN, ROLE_OPERATOR, require_role
from app.services import audit_service, notify_service

router = APIRouter(prefix="/api/system/notifications", tags=["notifications"])


@router.get("/config", response_model=ApiResponse[NotificationConfigOut])
async def get_notification_config(
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_role(ROLE_ADMIN)),
):
    config = await notify_service.get_or_create_config(session)
    await session.commit()
    return ApiResponse(data=notify_service.to_out(config))


@router.patch("/config", response_model=ApiResponse[NotificationConfigOut])
async def update_notification_config(
    body: NotificationConfigUpdate,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    config, changed = await notify_service.update_config(
        session, body, operator=user.get("sub", "system")
    )
    # 审计只写字段名：URL/密钥/口令绝不进入审计明文
    await audit_service.log(
        session,
        operator=user.get("sub", "system"),
        action=audit_service.ACTION_NOTIFY_CONFIG_UPDATE,
        target_type="notification_config",
        target_id=str(notify_service.CONFIG_ID),
        detail={"fields": changed},
    )
    await session.commit()
    return ApiResponse(data=notify_service.to_out(config))


@router.post("/test", response_model=ApiResponse[list[NotificationTestResult]])
async def test_notification(
    body: NotificationTestRequest,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    """真实发送测试通知：忽略规则与静默，只验证通道连通性。"""
    results = await notify_service.send_test_notification(
        body.channel, user.get("sub", "tester"), session
    )
    return ApiResponse(data=results)


@router.get("/deliveries", response_model=ApiResponse[list[NotificationDeliveryOut]])
async def list_notification_deliveries(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=notify_service.MAX_DELIVERY_PAGE),
    status: Optional[str] = Query(None, pattern="^(sent|failed|suppressed)$"),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_role(ROLE_ADMIN)),
):
    rows, _ = await notify_service.list_deliveries(session, page, page_size, status)
    return ApiResponse(data=rows)
