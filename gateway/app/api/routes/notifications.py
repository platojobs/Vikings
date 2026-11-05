"""告警通知配置、规则与投递历史（v1.31.0）。

安全约定：
- GET 配置/规则只返回「是否已配置」，不回显 URL / 签名密钥 / SMTP 口令；
- 规则写入审计只记录规则 ID、名称与变更字段名，不记录值；
- 测试通知真实调用通道，结果逐通道返回；
- 投递历史对 operator 开放（值班排障），viewer 不可见（错误信息含接收端基础设施线索）。
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import (
    ApiResponse,
    NotificationConfigOut,
    NotificationConfigUpdate,
    NotificationDeliveryPageOut,
    NotificationRuleCreate,
    NotificationRuleOut,
    NotificationRuleUpdate,
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


@router.get("/rules", response_model=ApiResponse[list[NotificationRuleOut]])
async def list_notification_rules(
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_role(ROLE_ADMIN)),
):
    rows = await notify_service.list_rules(session)
    return ApiResponse(data=[notify_service.rule_to_out(row) for row in rows])


@router.post("/rules", response_model=ApiResponse[NotificationRuleOut])
async def create_notification_rule(
    body: NotificationRuleCreate,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    operator = user.get("sub", "system")
    try:
        rule = await notify_service.create_rule(session, body, operator)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_NOTIFY_RULE_CREATE,
        target_type="notification_rule",
        target_id=str(rule.id),
        detail={"rule_id": rule.id, "name": rule.name, "priority": rule.priority},
    )
    await session.commit()
    return ApiResponse(data=notify_service.rule_to_out(rule))


@router.patch("/rules/{rule_id}", response_model=ApiResponse[NotificationRuleOut])
async def update_notification_rule(
    rule_id: int,
    body: NotificationRuleUpdate,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    operator = user.get("sub", "system")
    try:
        rule, changed = await notify_service.update_rule(session, rule_id, body, operator)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if rule is None:
        raise HTTPException(status_code=404, detail="通知规则不存在")
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_NOTIFY_RULE_UPDATE,
        target_type="notification_rule",
        target_id=str(rule.id),
        detail={"rule_id": rule.id, "name": rule.name, "fields": changed},
    )
    await session.commit()
    return ApiResponse(data=notify_service.rule_to_out(rule))


@router.delete("/rules/{rule_id}", response_model=ApiResponse[dict])
async def delete_notification_rule(
    rule_id: int,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    rule = await notify_service.delete_rule(session, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="通知规则不存在")
    await audit_service.log(
        session,
        operator=user.get("sub", "system"),
        action=audit_service.ACTION_NOTIFY_RULE_DELETE,
        target_type="notification_rule",
        target_id=str(rule_id),
        detail={"rule_id": rule_id, "name": rule.name},
    )
    await session.commit()
    return ApiResponse(data={"deleted": rule_id})


@router.post("/test", response_model=ApiResponse[list[NotificationTestResult]])
async def test_notification(
    body: NotificationTestRequest,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    """真实发送测试通知：忽略规则匹配与静默，只验证通道连通性。"""
    results = await notify_service.send_test_notification(
        body.channel, user.get("sub", "tester"), session, rule_id=body.rule_id
    )
    return ApiResponse(data=results)


@router.get("/deliveries", response_model=ApiResponse[NotificationDeliveryPageOut])
async def list_notification_deliveries(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=notify_service.MAX_DELIVERY_PAGE),
    status: Optional[str] = Query(None, pattern="^(pending|sent|failed|suppressed)$"),
    channel: Optional[str] = Query(None, pattern="^(webhook|smtp|mute|rule)$"),
    device_id: Optional[str] = Query(None, max_length=64),
    start: Optional[datetime] = Query(None),
    end: Optional[datetime] = Query(None),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_role(ROLE_OPERATOR)),
):
    """投递历史（v1.31.0）：分页与筛选全部在服务端完成，语义与列表一致。"""
    data = await notify_service.list_deliveries(
        session,
        page=page,
        page_size=page_size,
        status=status,
        channel=channel,
        device_id=device_id,
        start=start,
        end=end,
    )
    return ApiResponse(data=data)
