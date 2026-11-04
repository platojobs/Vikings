"""设备路由：注册/编辑/删除、列表 / 详情 / 历史数据 / 数据聚合 / 指令下发与查询。"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import (
    AggregatePoint,
    ApiResponse,
    CommandCreateRequest,
    CommandOut,
    DeviceBulkCreateRequest,
    DeviceBulkResult,
    DeviceCreateRequest,
    DeviceDetailOut,
    DeviceGroupOut,
    DeviceOut,
    DeviceUpdateRequest,
    PageOut,
    SensorRecordOut,
    to_utc_datetime,
)
from app.security import ROLE_ADMIN, ROLE_OPERATOR, require_role, require_user
from app.services import audit_service, command_service, device_service

router = APIRouter(prefix="/api", tags=["devices"])


@router.get("/devices", response_model=ApiResponse[PageOut[DeviceOut]])
async def list_devices(
    status: Optional[str] = Query(default=None, pattern="^(online|offline)$"),
    keyword: Optional[str] = Query(default=None, max_length=64),
    group: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    items, total = await device_service.list_devices(
        session, status, keyword, page, pageSize, group
    )
    return ApiResponse(
        data=PageOut[DeviceOut](
            items=[DeviceOut.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=pageSize,
        )
    )


@router.get("/devices/groups", response_model=ApiResponse[list[DeviceGroupOut]])
async def list_device_groups(
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    """分组概览：默认分组也会统计，前端用于下拉筛选与数量提示。"""
    groups = await device_service.list_groups(session)
    return ApiResponse(data=[DeviceGroupOut(**item) for item in groups])


@router.post("/devices", response_model=ApiResponse[DeviceOut])
async def create_device(
    body: DeviceCreateRequest,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    device, err = await device_service.create_device(
        session,
        body.device_id,
        body.name,
        body.model,
        body.firmware_version,
        body.group_name,
    )
    if device is None:
        raise HTTPException(status_code=409, detail=err)

    await audit_service.log(
        session,
        operator=user["sub"],
        action=audit_service.ACTION_DEVICE_CREATE,
        target_type="device",
        target_id=device.device_id,
        detail={"name": device.name, "group": device.group_name},
    )
    await session.commit()
    return ApiResponse(data=DeviceOut.model_validate(device))


@router.post("/devices/bulk", response_model=ApiResponse[DeviceBulkResult])
async def bulk_create_devices(
    body: DeviceBulkCreateRequest,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    """批量注册设备：返回成功与跳过明细，重复 ID 不会导致整批失败。"""
    created, skipped = await device_service.bulk_create_devices(
        session, [item.model_dump() for item in body.devices]
    )
    await audit_service.log(
        session,
        operator=user["sub"],
        action=audit_service.ACTION_DEVICE_BULK,
        target_type="device",
        target_id=",".join(created[:5]),
        detail={"created": len(created), "skipped": len(skipped)},
    )
    await session.commit()
    return ApiResponse(data=DeviceBulkResult(created=created, skipped=skipped))


@router.patch("/devices/{device_id}", response_model=ApiResponse[DeviceOut])
async def update_device(
    device_id: str,
    body: DeviceUpdateRequest,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    changes = {key: value for key, value in body.model_dump().items() if value is not None}
    device, err = await device_service.update_device(session, device_id, changes)
    if device is None:
        raise HTTPException(status_code=404, detail=err)

    await audit_service.log(
        session,
        operator=user["sub"],
        action=audit_service.ACTION_DEVICE_UPDATE,
        target_type="device",
        target_id=device_id,
        detail=changes,
    )
    await session.commit()
    return ApiResponse(data=DeviceOut.model_validate(device))


@router.delete("/devices/{device_id}", response_model=ApiResponse[dict])
async def delete_device(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    """删除设备：仅管理员可操作，且要求该设备没有历史时序数据。"""
    ok, err = await device_service.delete_device(session, device_id)
    if not ok:
        status = 404 if "不存在" in err else 409
        raise HTTPException(status_code=status, detail=err)

    await audit_service.log(
        session,
        operator=user["sub"],
        action=audit_service.ACTION_DEVICE_DELETE,
        target_type="device",
        target_id=device_id,
    )
    await session.commit()
    return ApiResponse(data={"deleted": device_id})


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


@router.get("/devices/{device_id}/records/aggregate", response_model=ApiResponse[list[AggregatePoint]])
async def device_records_aggregate(
    device_id: str,
    bucket: str = Query(default="hour", pattern="^(hour|day)$"),
    start: Optional[str] = Query(default=None, description="ISO8601 或毫秒时间戳"),
    end: Optional[str] = Query(default=None, description="ISO8601 或毫秒时间戳"),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    """按小时/天聚合设备时序数据，用于趋势图与报表，避免前端拉全量明细。"""
    try:
        start_dt = to_utc_datetime(start) if start else None
        end_dt = to_utc_datetime(end) if end else None
    except ValueError:
        raise HTTPException(status_code=400, detail="时间格式非法，请使用 ISO8601 或毫秒时间戳")

    rows = await device_service.aggregate_records(session, device_id, bucket, start_dt, end_dt)
    return ApiResponse(data=[AggregatePoint.model_validate(row) for row in rows])


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
    user: dict = Depends(require_role(ROLE_OPERATOR)),
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
    await audit_service.log(
        session,
        operator=user.get("sub", "system"),
        action=audit_service.ACTION_COMMAND_CREATE,
        target_type="device",
        target_id=device_id,
        detail={"cmdId": cmd.cmd_id, "type": cmd.type, "payload": cmd.payload},
    )
    await session.commit()
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
