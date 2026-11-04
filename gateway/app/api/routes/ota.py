"""OTA 路由：创建任务、任务列表、任务详情（含每台设备升级状态）。"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import ApiResponse, OtaTaskCreateRequest, OtaTaskOut, PageOut
from app.security import ROLE_OPERATOR, require_role, require_user
from app.services import audit_service, command_service

router = APIRouter(prefix="/api/ota", tags=["ota"])


@router.post("/tasks", response_model=ApiResponse[OtaTaskOut])
async def create_task(
    body: OtaTaskCreateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    mqtt = request.app.state.mqtt
    if not mqtt.is_connected:
        raise HTTPException(status_code=503, detail="MQTT Broker 未连接，无法下发 OTA 任务")

    task = await command_service.create_ota_task(
        session=session,
        publisher=mqtt.publish,
        device_ids=body.device_ids,
        target_version=body.target_version,
        url=body.url,
        description=body.description,
        operator=user.get("sub", "system"),
    )
    await audit_service.log(
        session,
        operator=user.get("sub", "system"),
        action=audit_service.ACTION_OTA_CREATE,
        target_type="ota_task",
        target_id=task.task_id,
        detail={
            "targetVersion": body.target_version,
            "deviceCount": len(body.device_ids),
            "deviceIds": body.device_ids[:20],
        },
    )
    await session.commit()
    return ApiResponse(data=task)


@router.get("/tasks", response_model=ApiResponse[PageOut[OtaTaskOut]])
async def list_tasks(
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=10, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    items, total = await command_service.list_ota_tasks(session, page, pageSize)
    return ApiResponse(
        data=PageOut[OtaTaskOut](items=items, total=total, page=page, page_size=pageSize)
    )


@router.get("/tasks/{task_id}", response_model=ApiResponse[OtaTaskOut])
async def task_detail(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    task = await command_service.get_ota_task(session, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="OTA 任务不存在")
    return ApiResponse(data=task)
