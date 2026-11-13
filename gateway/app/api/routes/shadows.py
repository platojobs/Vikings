"""设备影子路由（v1.36.0）。

端点：
- GET    /devices/{device_id}/shadow            读取影子（期望/上报/差异/同步状态）
- PUT    /devices/{device_id}/shadow/desired    合并写入期望（admin/operator）
- DELETE /devices/{device_id}/shadow/desired    清除指定键的期望（admin/operator）
- POST   /devices/{device_id}/shadow/reported   设备端上报实际状态（admin/operator；REST）
- POST   /devices/shadow/desired/batch          批量设置期望（admin/operator，复用标签筛选）
- GET    /devices/{device_id}/shadow/history    查询不可变期望版本快照
- POST   /devices/{device_id}/shadow/rollback   精确回滚到指定快照并生成新版本
"""

from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import (
    ApiResponse,
    DeviceShadowBatchItem,
    DeviceShadowBatchResult,
    DeviceShadowDesiredBatchRequest,
    DeviceShadowDesiredUpdate,
    DeviceShadowOut,
    DeviceShadowReportedUpdate,
    DeviceShadowHistoryOut,
    DeviceShadowRevisionOut,
    DeviceShadowRollbackRequest,
)
from app.security import ROLE_OPERATOR, require_role, require_user
from app.services import audit_service, device_service, shadow_service

router = APIRouter(prefix="/api", tags=["device-shadow"])


async def _load_device(session: AsyncSession, device_id: str):
    device = await device_service.get_device(session, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"设备 {device_id} 不存在")
    return device


@router.get(
    "/devices/{device_id}/shadow",
    response_model=ApiResponse[DeviceShadowOut],
)
async def get_device_shadow(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    """读取设备影子：含期望、上报、差异键、同步状态。首次访问会创建空影子行。"""
    device = await _load_device(session, device_id)
    row = await shadow_service.get_shadow(session, device_id)
    return ApiResponse(data=DeviceShadowOut(**shadow_service.build_out(row, device.status)))


@router.get(
    "/devices/{device_id}/shadow/history",
    response_model=ApiResponse[DeviceShadowHistoryOut],
)
async def get_device_shadow_history(
    device_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    """查询期望版本快照；所有登录角色只读，分页不改变既有影子语义。"""
    await _load_device(session, device_id)
    items, total = await shadow_service.list_revisions(
        session, device_id, limit=limit, offset=offset
    )
    return ApiResponse(
        data=DeviceShadowHistoryOut(
            items=[DeviceShadowRevisionOut.model_validate(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )
    )


@router.put(
    "/devices/{device_id}/shadow/desired",
    response_model=ApiResponse[DeviceShadowOut],
)
async def update_device_shadow_desired(
    device_id: str,
    body: DeviceShadowDesiredUpdate,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    """合并写入期望状态（merge patch）。

    - 值为 null 表示清除该键；
    - 真正发生变化才递增版本号并写审计；
    - 设备离线时也允许写入，待设备上线后由其主动拉取。
    """
    device = await _load_device(session, device_id)
    try:
        row, changed = await shadow_service.apply_desired_patch(
            session, device_id, body.patch, user.get("sub", "system")
        )
    except shadow_service.ShadowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if changed:
        await audit_service.log(
            session,
            operator=user.get("sub", "system"),
            action=audit_service.ACTION_DEVICE_SHADOW_DESIRED,
            target_type="device_shadow",
            target_id=device_id,
            detail={
                "mode": "merge",
                "keys": sorted(body.patch.keys()),
                "clearedKeys": sorted([k for k, v in body.patch.items() if v is None]),
                "version": int(row.version or 0),
                "changed": True,
            },
        )
        await session.commit()
    return ApiResponse(data=DeviceShadowOut(**shadow_service.build_out(row, device.status)))


@router.delete(
    "/devices/{device_id}/shadow/desired",
    response_model=ApiResponse[DeviceShadowOut],
)
async def clear_device_shadow_desired(
    device_id: str,
    keys: List[str] = Query(default=[], description="要清除的期望键，可重复"),
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    """清除指定键的期望（operator/admin）。keys 为空时返回 400。"""
    if not keys:
        raise HTTPException(status_code=400, detail="请至少提供一个要清除的期望键")
    device = await _load_device(session, device_id)
    row, removed = await shadow_service.clear_desired_keys(
        session, device_id, keys, user.get("sub", "system")
    )
    if removed:
        await audit_service.log(
            session,
            operator=user.get("sub", "system"),
            action=audit_service.ACTION_DEVICE_SHADOW_DESIRED,
            target_type="device_shadow",
            target_id=device_id,
            detail={
                "mode": "clear",
                "keys": sorted(removed),
                "version": int(row.version or 0),
                "changed": True,
            },
        )
        await session.commit()
    return ApiResponse(data=DeviceShadowOut(**shadow_service.build_out(row, device.status)))


@router.post(
    "/devices/{device_id}/shadow/rollback",
    response_model=ApiResponse[DeviceShadowOut],
)
async def rollback_device_shadow(
    device_id: str,
    body: DeviceShadowRollbackRequest,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    """回滚到完整历史快照；生成新版本，绝不覆盖目标版本或重写历史。"""
    device = await _load_device(session, device_id)
    try:
        row, changed, changed_keys = await shadow_service.rollback_desired(
            session, device_id, body.version, user.get("sub", "system")
        )
    except shadow_service.ShadowRevisionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if changed:
        await audit_service.log(
            session,
            operator=user.get("sub", "system"),
            action=audit_service.ACTION_DEVICE_SHADOW_ROLLBACK,
            target_type="device_shadow",
            target_id=device_id,
            detail={
                "sourceVersion": body.version,
                "version": int(row.version or 0),
                "keys": changed_keys,
                "changed": True,
            },
        )
        await session.commit()
    return ApiResponse(data=DeviceShadowOut(**shadow_service.build_out(row, device.status)))


@router.post(
    "/devices/{device_id}/shadow/reported",
    response_model=ApiResponse[DeviceShadowOut],
)
async def report_device_shadow(
    device_id: str,
    body: DeviceShadowReportedUpdate,
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_role(ROLE_OPERATOR)),
):
    """设备端上报实际状态（当前版本使用 REST 主动上报，未新增影子 MQTT Topic）。

    上报载荷：`reported`（merge 语义）+ `version`（设备刚应用的 desired 版本号）。
    """
    device = await _load_device(session, device_id)
    try:
        row = await shadow_service.apply_reported(
            session, device_id, body.reported, body.version
        )
    except shadow_service.ShadowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await session.commit()
    return ApiResponse(data=DeviceShadowOut(**shadow_service.build_out(row, device.status)))


@router.post(
    "/devices/shadow/desired/batch",
    response_model=ApiResponse[DeviceShadowBatchResult],
)
async def batch_update_shadow_desired(
    body: DeviceShadowDesiredBatchRequest,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    """批量设置期望状态：复用标签筛选 + 分组筛选 + 显式 device_ids。

    - 三种条件可组合（取交集），不传任何条件返回 400；
    - 逐设备返回真实状态（updated / unchanged / not_found），不静默成功；
    - 仅在至少一台设备变更时写一条聚合审计。
    """
    if not body.device_ids and not body.tags and not body.group:
        raise HTTPException(
            status_code=400,
            detail="请至少提供 device_ids / tags / group 中的一种条件来圈选目标设备",
        )
    try:
        shadow_service._validate_patch(body.patch, field="patch")  # noqa: SLF001
        existing, missing = await shadow_service.resolve_target_devices(
            session, body.device_ids, body.tags, body.group
        )
        if len(existing) > shadow_service.MAX_BATCH:
            raise shadow_service.ShadowValidationError(
                f"单次批量最多 {shadow_service.MAX_BATCH} 台设备，当前命中 {len(existing)}"
            )
        results_raw = await shadow_service.batch_apply_desired(
            session, existing, body.patch, user.get("sub", "system")
        )
    except shadow_service.ShadowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    items: List[DeviceShadowBatchItem] = [
        DeviceShadowBatchItem(**r) for r in results_raw
    ] + [
        DeviceShadowBatchItem(device_id=d, status="not_found", version=0, error="设备不存在")
        for d in missing
    ]
    updated = sum(1 for i in items if i.status == "updated")
    unchanged = sum(1 for i in items if i.status == "unchanged")

    if updated:
        await audit_service.log(
            session,
            operator=user.get("sub", "system"),
            action=audit_service.ACTION_DEVICE_SHADOW_DESIRED_BATCH,
            target_type="device_shadow",
            target_id="batch",
            detail={
                "matched": len(existing),
                "deviceIds": bool(body.device_ids),
                "tags": sorted(set(body.tags or [])),
                "group": body.group or None,
                "keys": sorted(body.patch.keys()),
                "updated": updated,
                "unchanged": unchanged,
                "notFound": len(missing),
            },
        )
    await session.commit()
    return ApiResponse(
        data=DeviceShadowBatchResult(
            results=items,
            matched=len(existing),
            updated=updated,
            unchanged=unchanged,
            not_found=len(missing),
        )
    )
