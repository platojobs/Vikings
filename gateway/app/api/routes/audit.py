"""审计日志路由：分页查询操作审计记录。"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import ApiResponse, AuditLogOut, PageOut
from app.security import require_user
from app.services import audit_service

router = APIRouter(prefix="/api/audit-logs", tags=["audit"])


@router.get("", response_model=ApiResponse[PageOut[AuditLogOut]])
async def list_audit_logs(
    operator: Optional[str] = Query(default=None, max_length=64),
    action: Optional[str] = Query(default=None, max_length=32),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    rows, total = await audit_service.list_logs(session, operator, action, page, pageSize)
    return ApiResponse(
        data=PageOut[AuditLogOut](
            items=[AuditLogOut.model_validate(row) for row in rows],
            total=total,
            page=page,
            page_size=pageSize,
        )
    )
