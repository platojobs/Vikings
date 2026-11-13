"""维护窗口路由（v1.39.0）。

权限：读取登录即可（operator/viewer 只读）；创建/修改/提前结束仅 admin；
未登录由 `require_user` 统一 401。
"""

import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import maintenance_window as maintenance
from app.database import get_session
from app.schemas import (
    ApiResponse,
    MaintenanceSuppressionOut,
    MaintenanceSummaryOut,
    MaintenanceWindowCreate,
    MaintenanceWindowOut,
    MaintenanceWindowUpdate,
    PageOut,
)
from app.security import require_role, require_user
from app.services import audit_service, maintenance_service
from app.security import ROLE_ADMIN

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


@router.get("/windows", response_model=ApiResponse[List[MaintenanceWindowOut]])
async def list_windows(
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    rows = await maintenance_service.list_windows(session)
    now = datetime.now(timezone.utc)
    return ApiResponse(data=[maintenance_service.to_out(row, now) for row in rows])


@router.post("/windows", response_model=ApiResponse[MaintenanceWindowOut])
async def create_window(
    body: MaintenanceWindowCreate,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    operator = user.get("sub", "system")
    try:
        row = await maintenance_service.create_window(
            session,
            name=body.name,
            reason=body.reason,
            scope=body.scope,
            mode=body.mode,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            recurrence=body.recurrence.model_dump() if body.recurrence else None,
            operator=operator,
        )
    except maintenance.MaintenanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_MAINTENANCE_CREATE,
        target_type="maintenance_window",
        target_id=row.window_id,
        detail={"window_id": row.window_id, "name": row.name, "mode": row.mode},
    )
    await session.refresh(row)
    data = maintenance_service.to_out(row)
    await session.commit()
    return ApiResponse(data=data)


@router.patch("/windows/{window_id}", response_model=ApiResponse[MaintenanceWindowOut])
async def update_window(
    window_id: str,
    body: MaintenanceWindowUpdate,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    operator = user.get("sub", "system")
    row = await maintenance_service.get_window(session, window_id)
    if row is None:
        raise HTTPException(status_code=404, detail="维护窗口不存在")
    provided = body.model_fields_set
    try:
        row, changed = await maintenance_service.update_window(
            session,
            row,
            name=body.name,
            reason=body.reason if "reason" in provided else None,
            scope=body.scope,
            scope_provided="scope" in provided,
            mode=body.mode,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            recurrence=body.recurrence.model_dump() if body.recurrence else None,
            recurrence_provided="recurrence" in provided,
        )
    except maintenance.MaintenanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_MAINTENANCE_UPDATE,
        target_type="maintenance_window",
        target_id=row.window_id,
        detail={"window_id": row.window_id, "name": row.name, "fields": changed},
    )
    await session.refresh(row)
    data = maintenance_service.to_out(row)
    await session.commit()
    return ApiResponse(data=data)


@router.post("/windows/{window_id}/end", response_model=ApiResponse[MaintenanceWindowOut])
async def end_window(
    window_id: str,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    operator = user.get("sub", "system")
    row = await maintenance_service.get_window(session, window_id)
    if row is None:
        raise HTTPException(status_code=404, detail="维护窗口不存在")
    summary = await maintenance_service.end_window(session, row, operator=operator)
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_MAINTENANCE_END,
        target_type="maintenance_window",
        target_id=row.window_id,
        detail={
            "window_id": row.window_id,
            "name": row.name,
            "summary": {
                "suppressed": summary.suppressed_count,
                "critical": summary.critical_suppressed,
            }
            if summary
            else None,
        },
    )
    await session.refresh(row)
    data = maintenance_service.to_out(row)
    await session.commit()
    return ApiResponse(data=data)


@router.get(
    "/windows/{window_id}/summaries",
    response_model=ApiResponse[List[MaintenanceSummaryOut]],
)
async def list_summaries(
    window_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    if await maintenance_service.get_window(session, window_id) is None:
        raise HTTPException(status_code=404, detail="维护窗口不存在")
    rows = await maintenance_service.list_summaries(session, window_id, limit=limit)
    return ApiResponse(data=[MaintenanceSummaryOut.model_validate(row, from_attributes=True) for row in rows])


@router.get(
    "/windows/{window_id}/suppressions",
    response_model=ApiResponse[PageOut[MaintenanceSuppressionOut]],
)
async def list_suppressions(
    window_id: str,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    if await maintenance_service.get_window(session, window_id) is None:
        raise HTTPException(status_code=404, detail="维护窗口不存在")
    rows, total = await maintenance_service.list_suppressions(
        session, window_id, page=page, page_size=pageSize
    )
    return ApiResponse(
        data=PageOut[MaintenanceSuppressionOut](
            items=[
                MaintenanceSuppressionOut.model_validate(row, from_attributes=True) for row in rows
            ],
            total=total,
            page=page,
            page_size=pageSize,
        )
    )
