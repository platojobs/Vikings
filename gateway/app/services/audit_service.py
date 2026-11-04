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
ACTION_COMMAND_CREATE = "command.create"
ACTION_OTA_CREATE = "ota.create"
ACTION_ALERT_ACK = "alert.ack"
ACTION_THRESHOLD_UPDATE = "threshold.update"
ACTION_USER_CREATE = "user.create"
ACTION_USER_UPDATE = "user.update"
ACTION_USER_DELETE = "user.delete"


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
