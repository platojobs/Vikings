"""设备路由：列表 / 详情 / 历史数据 / 指令下发与查询。"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import (
    ApiResponse,
    CommandCreateRequest,
    CommandOut,
    DeviceDetailOut,
    DeviceOut,
    PageOut,
    SensorRecordOut,
    to_utc_datetime,
)
from app.security import require_user
from app.services import command_service, device_service

router = APIRouter(prefix="/api", tags=["devices"])


@router.get("/devices", response_model=ApiResponse[PageOut[DeviceOut]])
async def list_devices(
    status: Optional[str] = Query(default=None, pattern="^(online|offline)$"),
    keyword: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    items, total = await device_service.list_devices(session, status, keyword, page, pageSize)
    return ApiResponse(
        data=PageOut[DeviceOut](
            items=[DeviceOut.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=pageSize,
        )
    )


@router.get("/devices/{device_id}", response_model=ApiResponse[DeviceDetailOut])
async def device_detail(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    device = await device_service.get_device(session, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")

    latest = await device_service.latest_record(session, device_id)
    detail = DeviceDetailOut.model_validate(device)
    detail.latest = SensorRecordOut.model_validate(latest) if latest else None
    return ApiResponse(data=detail)


@router.get("/devices/{device_id}/records", response_model=ApiResponse[list[SensorRecordOut]])
async def device_records(
    device_id: str,
    start: Optional[str] = Query(default=None, description="ISO8601 或毫秒时间戳"),
    end: Optional[str] = Query(default=None, description="ISO8601 或毫秒时间戳"),
    limit: int = Query(default=500, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    try:
        start_dt = to_utc_datetime(start) if start else None
        end_dt = to_utc_datetime(end) if end else None
    except ValueError:
        raise HTTPException(status_code=400, detail="时间格式非法，请使用 ISO8601 或毫秒时间戳")

    rows = await device_service.query_records(session, device_id, start_dt, end_dt, limit)
    return ApiResponse(data=[SensorRecordOut.model_validate(row) for row in rows])


@router.get("/records/latest", response_model=ApiResponse[list[SensorRecordOut]])
async def latest_records(
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    rows = await device_service.latest_records_all(session)
    return ApiResponse(data=[SensorRecordOut.model_validate(row) for row in rows])


@router.post("/devices/{device_id}/commands", response_model=ApiResponse[CommandOut])
async def create_command(
    device_id: str,
    body: CommandCreateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_user),
):
    mqtt = request.app.state.mqtt
    cmd, err = await command_service.issue_command(
        session=session,
        publisher=mqtt.publish,
        device_id=device_id,
        cmd_type=body.type,
        payload=body.payload,
        operator=user.get("sub", "system"),
    )
    if cmd is None:
        raise HTTPException(status_code=404, detail=err or "设备不存在")
    if err:
        raise HTTPException(status_code=503, detail=err)
    return ApiResponse(data=CommandOut.model_validate(cmd))


@router.get("/devices/{device_id}/commands", response_model=ApiResponse[list[CommandOut]])
async def device_commands(
    device_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    rows = await command_service.list_commands(session, device_id, limit)
    return ApiResponse(data=[CommandOut.model_validate(row) for row in rows])
