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
from app.services import alert_service, audit_service, command_service, device_service

router = APIRouter(prefix="/api", tags=["devices"])


@router.get("/devices", response_model=ApiResponse[PageOut[DeviceOut]])
async def list_devices(
    status: Optional[str] = Query(default=None, pattern="^(online|offline)$"),
    keyword: Optional[str] = Query(default=None, max_length=64),
    group: Optional[str] = Query(default=None, max_length=64),
    hasAlert: bool = Query(default=False, description="仅返回存在未处理告警的设备"),
    withAlertCount: bool = Query(default=False, description="返回每设备未处理告警数"),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    """设备列表。

    hasAlert / withAlertCount 均为可选且默认关闭：不传时返回结构与语义与历史一致。
    告警计数在数据库层按设备聚合，不受 `/api/alerts` 的 pageSize 截断影响。
    """
    items, total = await device_service.list_devices(
        session, status, keyword, page, pageSize, group, has_alert=hasAlert
    )
    counts = {}
    if withAlertCount:
        counts = await alert_service.open_alert_counts_by_device(
            session, [item.device_id for item in items]
        )
    out_items = []
    for item in items:
        out = DeviceOut.model_validate(item)
        if withAlertCount:
            out.open_alert_count = counts.get(item.device_id, 0)
        out_items.append(out)
    return ApiResponse(
        data=PageOut[DeviceOut](
            items=out_items,
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
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    changes = {key: value for key, value in body.model_dump().items() if value is not None}
    device, err, group_event = await device_service.update_device_with_group_event(
        session, device_id, changes, user.get("sub", "system")
    )
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

    # 提交后再发布，订阅端回查 REST 时一定看到新分组；发布失败不影响编辑结果
    if group_event is not None:
        await device_service.publish_group_change(
            request.app.state.mqtt.publish, group_event
        )
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
    offset: int = Query(default=0, ge=0, description="从最新一条开始跳过的条数，用于向更早翻页"),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    """单设备历史数据。

    offset 为可选增量参数，默认 0：不传时返回结构与语义与历史一致（最近 limit 条、时间正序）。
    传 offset 时按时间倒序跳过对应条数，用于详情页「加载更早」分页。
    """
    try:
        start_dt = to_utc_datetime(start) if start else None
        end_dt = to_utc_datetime(end) if end else None
    except ValueError:
        raise HTTPException(status_code=400, detail="时间格式非法，请使用 ISO8601 或毫秒时间戳")

    rows = await device_service.query_records(
        session, device_id, start_dt, end_dt, limit, offset
    )
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
    status: Optional[str] = Query(default=None, pattern="^(online|offline)$"),
    keyword: Optional[str] = Query(default=None, max_length=64),
    limit: Optional[int] = Query(default=None, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    deviceIds: Optional[str] = Query(
        default=None,
        max_length=8192,
        description="逗号分隔的设备 ID，仅返回这些设备的最新值（最多 200 个）",
    ),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    """全局最新值。

    status / keyword / limit / offset / deviceIds 均为可选参数；不传时返回结构与语义
    与历史一致（全部设备各自最新一条、按时间倒序、不截断），因此老客户端无需改动。
    """
    device_ids = None
    if deviceIds is not None:
        device_ids = []
        for part in deviceIds.split(","):
            part = part.strip()
            if part and part not in device_ids:
                device_ids.append(part)
        if len(device_ids) > 200:
            raise HTTPException(status_code=400, detail="deviceIds 单次最多 200 个")
    rows = await device_service.latest_records_all(
        session, status, keyword, limit, offset, device_ids
    )
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
