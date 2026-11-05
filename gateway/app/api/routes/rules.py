"""告警规则路由（v1.34.0）：规则 CRUD、启停与旧阈值接口兼容层。

权限：
- GET 列表 / GET 阈值：登录可读（viewer / operator 只读）；
- 写入（新增 / 修改 / 启停 / 删除 / 恢复默认）：仅 admin；
- 未登录由 require_user 统一返回 401。

审计：只记录规则 ID、名称与变更字段名，不记录阈值等具体数值（见 docs/contracts.md）。
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import (
    AlertRuleCreate,
    AlertRuleEnabledUpdate,
    AlertRuleOut,
    AlertRuleUpdate,
    ApiResponse,
    ThresholdsUpdateRequest,
)
from app.security import ROLE_ADMIN, require_role, require_user
from app.services import alert_rule_service, audit_service, rule_service
from app.services.alert_rule_service import RuleValidationError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/rules", tags=["rules"])


def _fields(payload) -> List[str]:
    return sorted(payload.model_dump(exclude_unset=True).keys())


@router.get("", response_model=ApiResponse[List[AlertRuleOut]])
async def list_alert_rules(
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    """规则列表：登录即可查看（operator / viewer 只读）。"""
    rows = await alert_rule_service.list_rules(session)
    return ApiResponse(data=[alert_rule_service.to_out(row) for row in rows])


@router.get("/thresholds")
async def get_thresholds(_: dict = Depends(require_user)):
    """兼容 v1.33.0：当前生效阈值，由默认规则推导，每项标注来源。"""
    return ApiResponse(data=rule_service.effective_view())


@router.put("/thresholds")
async def update_thresholds(
    body: ThresholdsUpdateRequest,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    """兼容 v1.33.0：在线更新阈值并即时刷新规则引擎（同步写默认规则）。

    必须声明在 `/{rule_id}` 之前，否则 FastAPI 会把 "thresholds" 当作 rule_id。
    """
    updated = await rule_service.update_thresholds(session, body, user["sub"])
    if updated:
        await audit_service.log(
            session,
            operator=user["sub"],
            action=audit_service.ACTION_THRESHOLD_UPDATE,
            target_type="rule_config",
            detail={"changes": updated},
        )
    await session.commit()
    return ApiResponse(data={"updated": updated, "thresholds": rule_service.effective_view()})


@router.post("", response_model=ApiResponse[AlertRuleOut])
async def create_alert_rule(
    body: AlertRuleCreate,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    """新增规则：admin 专用；阈值方向/范围/NaN 校验失败返回 400。"""
    operator = user.get("sub", "system")
    try:
        rule = await alert_rule_service.create_rule(session, body, operator)
    except RuleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_ALERT_RULE_CREATE,
        target_type="alert_rule",
        target_id=str(rule.id),
        detail={"name": rule.name, "fields": _fields(body)},
    )
    out = alert_rule_service.to_out(rule)
    await session.commit()
    return ApiResponse(data=out)


@router.post("/restore-defaults", response_model=ApiResponse[dict])
async def restore_alert_rules(
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    """把默认规则阈值回滚到环境变量/出厂值（自定义规则不动）。"""
    operator = user.get("sub", "system")
    changed = await alert_rule_service.restore_defaults(session, operator)
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_ALERT_RULE_RESTORE,
        target_type="alert_rule",
        target_id="defaults",
        detail={"changed_fields": ["threshold"], "count": changed},
    )
    await session.commit()
    return ApiResponse(data={"restored": changed})


@router.put("/{rule_id}", response_model=ApiResponse[AlertRuleOut])
async def update_alert_rule(
    rule_id: int,
    body: AlertRuleUpdate,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    operator = user.get("sub", "system")
    try:
        rule, changed = await alert_rule_service.update_rule(session, rule_id, body, operator)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_ALERT_RULE_UPDATE,
        target_type="alert_rule",
        target_id=str(rule.id),
        detail={"name": rule.name, "changed_fields": changed},
    )
    out = alert_rule_service.to_out(rule)
    await session.commit()
    return ApiResponse(data=out)


@router.patch("/{rule_id}/enabled", response_model=ApiResponse[AlertRuleOut])
async def set_alert_rule_enabled(
    rule_id: int,
    body: AlertRuleEnabledUpdate,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    """启用/停用：停用后该规则不再产生新告警，既有告警与统计不受影响。"""
    operator = user.get("sub", "system")
    try:
        rule = await alert_rule_service.set_enabled(session, rule_id, body.enabled, operator)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await audit_service.log(
        session,
        operator=operator,
        action=(
            audit_service.ACTION_ALERT_RULE_ENABLE
            if body.enabled
            else audit_service.ACTION_ALERT_RULE_DISABLE
        ),
        target_type="alert_rule",
        target_id=str(rule.id),
        detail={"name": rule.name, "changed_fields": ["enabled"]},
    )
    out = alert_rule_service.to_out(rule)
    await session.commit()
    return ApiResponse(data=out)


@router.delete("/{rule_id}", response_model=ApiResponse[dict])
async def delete_alert_rule(
    rule_id: int,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    operator = user.get("sub", "system")
    try:
        rule = await alert_rule_service.delete_rule(session, rule_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_ALERT_RULE_DELETE,
        target_type="alert_rule",
        target_id=str(rule_id),
        detail={"rule_id": rule_id, "name": rule.name},
    )
    await session.commit()
    return ApiResponse(data={"deleted": rule_id})
