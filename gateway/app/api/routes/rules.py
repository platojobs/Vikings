"""告警规则路由（v1.34.0）：规则 CRUD、启停与旧阈值接口兼容层。

权限：
- GET 列表 / GET 阈值：登录可读（viewer / operator 只读）；
- 写入（新增 / 修改 / 启停 / 删除 / 恢复默认）：仅 admin；
- 未登录由 require_user 统一返回 401。

审计：记录规则 ID、名称与变更字段名，并在 `changes` 中保存逐字段前后值
（v1.35.0 起用于「规则变更历史」；规则字段本身不含密钥/口令，见 docs/contracts.md）。
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import AuditLog
from app.schemas import (
    AlertRuleCreate,
    AlertRuleEnabledUpdate,
    AlertRuleHistoryEntry,
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
        detail={
            "name": rule.name,
            "fields": _fields(body),
            "changes": {
                field: {"from": None, "to": value}
                for field, value in alert_rule_service.to_out(rule).items()
                if field in set(_fields(body))
            },
        },
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
    changes = await alert_rule_service.restore_defaults(session, operator)
    for item in changes:
        await audit_service.log(
            session,
            operator=operator,
            action=audit_service.ACTION_ALERT_RULE_RESTORE,
            target_type="alert_rule",
            target_id=str(item["rule_id"]),
            detail={
                "name": item["name"],
                "changed_fields": ["threshold"],
                "changes": {"threshold": {"from": item["from"], "to": item["to"]}},
            },
        )
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_ALERT_RULE_RESTORE,
        target_type="alert_rule",
        target_id="defaults",
        detail={"changed_fields": ["threshold"], "count": len(changes)},
    )
    await session.commit()
    return ApiResponse(data={"restored": len(changes)})


@router.get("/{rule_id}/history", response_model=ApiResponse[List[AlertRuleHistoryEntry]])
async def alert_rule_history(
    rule_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    """规则变更历史：直接读 `audit_log` 中 `target_type=alert_rule` 的记录。

    不新造平行记录表；`changes` 提供逐字段前后值（规则字段不含密钥）。
    规则被删除后历史仍可查（审计保留），因此不校验规则是否存在。
    """
    stmt = (
        select(AuditLog)
        .where(
            AuditLog.target_type == "alert_rule",
            AuditLog.target_id == str(rule_id),
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    entries = [
        AlertRuleHistoryEntry(
            id=row.id,
            action=row.action,
            operator=row.operator,
            created_at=row.created_at,
            fields=list((row.detail or {}).get("changed_fields") or (row.detail or {}).get("fields") or []),
            changes=(row.detail or {}).get("changes") or {},
        )
        for row in rows
    ]
    return ApiResponse(data=entries)


@router.put("/{rule_id}", response_model=ApiResponse[AlertRuleOut])
async def update_alert_rule(
    rule_id: int,
    body: AlertRuleUpdate,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    operator = user.get("sub", "system")
    try:
        rule, changed, change_map = await alert_rule_service.update_rule(
            session, rule_id, body, operator
        )
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
        detail={"name": rule.name, "changed_fields": changed, "changes": change_map},
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
        rule, before = await alert_rule_service.set_enabled(
            session, rule_id, body.enabled, operator
        )
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
        detail={
            "name": rule.name,
            "changed_fields": ["enabled"],
            "changes": {"enabled": {"from": before, "to": body.enabled}},
        },
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
    snapshot = {
        key: alert_rule_service.to_jsonable(value)
        for key, value in alert_rule_service.to_out(rule).items()
    }
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_ALERT_RULE_DELETE,
        target_type="alert_rule",
        target_id=str(rule_id),
        detail={
            "rule_id": rule_id,
            "name": rule.name,
            "changed_fields": ["deleted"],
            "changes": {"deleted": {"from": snapshot, "to": None}},
        },
    )
    await session.commit()
    return ApiResponse(data={"deleted": rule_id})
