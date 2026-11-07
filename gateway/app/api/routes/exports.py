"""CSV 导出路由（v1.30.0）。

两个端点，复用列表筛选语义，服务端流式输出：
- `GET /api/exports/records`：设备历史上报；
- `GET /api/exports/alerts`：告警。

权限口径：operator / admin 可导出，viewer 不可导出。浏览是系统内只读，
批量导出会把数据复制到系统外（U 盘、邮件、工单），泄露面更大，因此需要
可承担操作责任的账号；导出动作写审计，便于追溯谁在什么时候导了什么范围。
"""

import asyncio
import logging
import time
from typing import AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.database import SessionFactory
from app.schemas import to_utc_datetime
from app.security import ROLE_OPERATOR, require_role
from app.services import audit_service, export_service

router = APIRouter(prefix="/api/exports", tags=["exports"])
logger = logging.getLogger(__name__)

ALERT_LEVEL_PATTERN = "^(warning|critical)$"
ALERT_STATUS_PATTERN = "^(open|acked)$"


def _parse_range(start: Optional[str], end: Optional[str]):
    try:
        start_dt = to_utc_datetime(start) if start else None
        end_dt = to_utc_datetime(end) if end else None
    except ValueError:
        raise HTTPException(status_code=400, detail="时间格式非法，请使用 ISO8601 或毫秒时间戳")
    if start_dt and end_dt and end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="结束时间需晚于开始时间")
    return start_dt, end_dt


def _audited_stream(
    inner: AsyncIterator[str],
    *,
    operator: str,
    action: str,
    filename: str,
    filters: Dict,
    started: float,
    state: Dict[str, int],
) -> AsyncIterator[str]:
    """包一层审计：客户端断开也会在 finally 落盘，且审计失败不影响已发送的数据。"""

    async def wrapper() -> AsyncIterator[str]:
        try:
            async for chunk in inner:
                yield chunk
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)

            async def _write_audit() -> None:
                try:
                    async with SessionFactory() as session:
                        await audit_service.log(
                            session,
                            operator=operator,
                            action=action,
                            target_type="export",
                            target_id=filename,
                            detail=export_service.audit_detail(
                                filters, state.get("rows", 0), duration_ms, filename
                            ),
                        )
                        await session.commit()
                except Exception as exc:  # 审计失败不能反过来破坏导出
                    logger.warning(
                        "导出审计写入失败 action=%s operator=%s error=%s",
                        action,
                        operator,
                        type(exc).__name__,
                    )

            task = asyncio.create_task(_write_audit())
            try:
                # 客户端断开时当前任务已被取消，shield 让审计任务继续写完
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass

    return wrapper()


@router.get("/records")
async def export_records(
    deviceId: Optional[str] = Query(default=None, max_length=64),
    group: Optional[str] = Query(default=None, max_length=64, description="设备分组"),
    tag: Optional[List[str]] = Query(
        default=None, max_length=32, description="设备标签，可重复；多个标签为「或」"
    ),
    start: Optional[str] = Query(default=None, description="ISO8601 或毫秒时间戳"),
    end: Optional[str] = Query(default=None, description="ISO8601 或毫秒时间戳"),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    started = time.perf_counter()
    start_dt, end_dt = _parse_range(start, end)
    settings = get_settings()

    async with SessionFactory() as session:
        total = await export_service.count_records(
            session, deviceId, group, start_dt, end_dt, tag
        )
    if total > settings.export_max_rows:
        raise HTTPException(
            status_code=422,
            detail=(
                f"导出 {total} 行超过上限 {settings.export_max_rows} 行，"
                "请缩小时间范围，或按设备 / 分组分批导出"
            ),
        )

    filename = export_service.build_filename("records", start_dt, end_dt)
    operator = user.get("sub", "system")
    state: Dict[str, int] = {"rows": 0}
    filters = {
        "deviceId": deviceId,
        "group": group,
        "tag": tag,
        "start": start_dt.isoformat() if start_dt else None,
        "end": end_dt.isoformat() if end_dt else None,
        "maxRows": settings.export_max_rows,
    }

    async def inner() -> AsyncIterator[str]:
        async with SessionFactory() as session:
            async for chunk in export_service.stream_records(
                session, deviceId, group, start_dt, end_dt, state, tag
            ):
                yield chunk

    return StreamingResponse(
        _audited_stream(
            inner(),
            operator=operator,
            action=audit_service.ACTION_EXPORT_RECORDS,
            filename=filename,
            filters=filters,
            started=started,
            state=state,
        ),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/alerts")
async def export_alerts(
    deviceId: Optional[str] = Query(default=None, max_length=64),
    group: Optional[str] = Query(default=None, max_length=64, description="设备分组"),
    level: Optional[str] = Query(default=None, pattern=ALERT_LEVEL_PATTERN),
    status: Optional[str] = Query(default=None, pattern=ALERT_STATUS_PATTERN),
    tag: Optional[List[str]] = Query(
        default=None, max_length=32, description="设备标签，可重复；多个标签为「或」"
    ),
    start: Optional[str] = Query(default=None, description="ISO8601 或毫秒时间戳"),
    end: Optional[str] = Query(default=None, description="ISO8601 或毫秒时间戳"),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    started = time.perf_counter()
    start_dt, end_dt = _parse_range(start, end)
    settings = get_settings()

    async with SessionFactory() as session:
        total = await export_service.count_alerts(
            session, deviceId, group, level, status, start_dt, end_dt, tag
        )
    if total > settings.export_max_rows:
        raise HTTPException(
            status_code=422,
            detail=(
                f"导出 {total} 行超过上限 {settings.export_max_rows} 行，"
                "请缩小时间范围，或按设备 / 分组分批导出"
            ),
        )

    filename = export_service.build_filename("alerts", start_dt, end_dt)
    operator = user.get("sub", "system")
    state: Dict[str, int] = {"rows": 0}
    filters = {
        "deviceId": deviceId,
        "group": group,
        "level": level,
        "status": status,
        "tag": tag,
        "start": start_dt.isoformat() if start_dt else None,
        "end": end_dt.isoformat() if end_dt else None,
        "maxRows": settings.export_max_rows,
    }

    async def inner() -> AsyncIterator[str]:
        async with SessionFactory() as session:
            async for chunk in export_service.stream_alerts(
                session, deviceId, group, level, status, start_dt, end_dt, state, tag
            ):
                yield chunk

    return StreamingResponse(
        _audited_stream(
            inner(),
            operator=operator,
            action=audit_service.ACTION_EXPORT_ALERTS,
            filename=filename,
            filters=filters,
            started=started,
            state=state,
        ),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
