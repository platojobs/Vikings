"""操作审计服务：所有改变系统状态的管理动作统一落审计日志。

打点点位：登录、下发指令、创建 OTA 任务、确认告警、修改阈值。
detail 字段存 JSON，记录动作上下文（如指令类型、载荷摘要）。
"""

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog

# 审计动作枚举（与前端展示文案对应）
ACTION_LOGIN = "login"
ACTION_LOGOUT = "logout"
ACTION_SESSION_REVOKE = "session.revoke"
ACTION_REFRESH_REUSE = "auth.refresh_reuse"
ACTION_COMMAND_CREATE = "command.create"
ACTION_COMMAND_BULK = "command.bulk_create"
ACTION_NOTIFY_CONFIG_UPDATE = "notify.config_update"
ACTION_NOTIFY_RULE_CREATE = "notify.rule_create"
ACTION_NOTIFY_RULE_UPDATE = "notify.rule_update"
ACTION_NOTIFY_RULE_DELETE = "notify.rule_delete"
ACTION_OTA_CREATE = "ota.create"
ACTION_ALERT_ACK = "alert.ack"
ACTION_ALERT_HANDOVER = "alert.handover"
ACTION_SLA_CONFIG_UPDATE = "sla.config_update"
ACTION_THRESHOLD_UPDATE = "threshold.update"
# v1.34.0 告警规则 CRUD：detail 只记录字段名，不记录阈值
ACTION_ALERT_RULE_CREATE = "alert_rule.create"
ACTION_ALERT_RULE_UPDATE = "alert_rule.update"
ACTION_ALERT_RULE_ENABLE = "alert_rule.enable"
ACTION_ALERT_RULE_DISABLE = "alert_rule.disable"
ACTION_ALERT_RULE_DELETE = "alert_rule.delete"
ACTION_ALERT_RULE_RESTORE = "alert_rule.restore_defaults"
ACTION_USER_CREATE = "user.create"
ACTION_USER_UPDATE = "user.update"
ACTION_USER_DELETE = "user.delete"
ACTION_DEVICE_CREATE = "device.create"
ACTION_DEVICE_UPDATE = "device.update"
# v1.35.0：批量打标签/移除标签（只记标签名与逐设备结果，设备标签本身不含敏感信息）
ACTION_DEVICE_TAGS = "device.tags_update"
# v1.36.0：设备影子期望写入（detail 只记键名与版本，不记值——值可能含设备控制语义）
ACTION_DEVICE_SHADOW_DESIRED = "device.shadow_desired_update"
ACTION_DEVICE_SHADOW_DESIRED_BATCH = "device.shadow_desired_batch"
# v1.37.0：回滚只记录来源/新版本与变化键名，快照值仍不进入审计
ACTION_DEVICE_SHADOW_ROLLBACK = "device.shadow_rollback"
ACTION_DEVICE_DELETE = "device.delete"
ACTION_DEVICE_BULK = "device.bulk_create"
ACTION_DEVICE_CREDENTIAL = "device.credential_issue"
ACTION_DEVICE_CREDENTIAL_REVOKE = "device.credential_revoke"
# v1.30.0 CSV 导出：记录筛选与行数，不记录导出内容
ACTION_EXPORT_RECORDS = "export.records"
ACTION_EXPORT_ALERTS = "export.alerts"


async def log(
    session: AsyncSession,
    operator: str,
    action: str,
    target_type: str = "",
    target_id: str = "",
    detail: Optional[Dict[str, Any]] = None,
) -> AuditLog:
    """写入一条审计日志（随调用方事务一起提交）。"""
    entry = AuditLog(
        operator=operator,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail or {},
    )
    session.add(entry)
    await session.flush()
    return entry


async def list_logs(
    session: AsyncSession,
    operator: Optional[str],
    action: Optional[str],
    page: int,
    page_size: int,
) -> Tuple[List[AuditLog], int]:
    stmt = select(AuditLog)
    count_stmt = select(func.count()).select_from(AuditLog)

    if operator:
        stmt = stmt.where(AuditLog.operator == operator)
        count_stmt = count_stmt.where(AuditLog.operator == operator)
    if action:
        stmt = stmt.where(AuditLog.action == action)
        count_stmt = count_stmt.where(AuditLog.action == action)

    stmt = stmt.order_by(AuditLog.created_at.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    total = int((await session.execute(count_stmt)).scalar_one())
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total
