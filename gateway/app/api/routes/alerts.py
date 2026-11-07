"""告警路由：列表、确认、SLA 配置与值班交接（v1.33.0）。"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_session
from app.observability import redact_text
from app.schemas import (
    AlertAckRequest,
    AlertBulkAckRequest,
    AlertBulkAckResult,
    AlertHandoverRequest,
    AlertHandoverResult,
    AlertOut,
    AlertSummaryOut,
    ApiResponse,
    PageOut,
    SlaConfigOut,
    SlaConfigUpdate,
    to_utc_datetime,
)
from app.security import ROLE_ADMIN, ROLE_OPERATOR, require_role, require_user
from app.services import alert_service, audit_service, notify_service, sla_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/alerts", tags=["alerts"])

# P2-1（v1.21.0）级别口径统一：规则引擎按阈值只产出 warning / critical，
# 对外枚举同步收窄为这两级，Web、移动端、契约文档一起收敛，不再保留空级别。
ALERT_LEVEL_PATTERN = "^(warning|critical)$"
# v1.33.0：SLA 三态口径 met/late/pending/overdue，另有 unknown（历史确认无处置时间）
ALERT_SLA_PATTERN = "^(met|late|pending|overdue|unknown)$"


def _note_summary(note: Optional[str]) -> Optional[str]:
    """审计只落脱敏后的备注摘要，避免口令/token 等敏感内容原样进审计与日志。"""
    if not note:
        return None
    return redact_text(note)[:100]


@router.get("", response_model=ApiResponse[PageOut[AlertOut]])
async def list_alerts(
    deviceId: Optional[str] = Query(default=None, max_length=64),
    level: Optional[str] = Query(default=None, pattern=ALERT_LEVEL_PATTERN),
    status: Optional[str] = Query(default=None, pattern="^(open|acked)$"),
    sla: Optional[str] = Query(default=None, pattern=ALERT_SLA_PATTERN, description="SLA 状态筛选"),
    tag: Optional[list[str]] = Query(
        default=None, max_length=32, description="设备标签，可重复；多个标签为「或」（v1.35.0）"
    ),
    start: Optional[str] = Query(default=None, description="创建时间下限，ISO8601 或毫秒"),
    end: Optional[str] = Query(default=None, description="创建时间上限，ISO8601 或毫秒"),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    try:
        start_dt = to_utc_datetime(start) if start else None
        end_dt = to_utc_datetime(end) if end else None
    except ValueError:
        raise HTTPException(status_code=400, detail="时间格式非法，请使用 ISO8601 或毫秒时间戳")
    if start_dt and end_dt and end_dt < start_dt:
        raise HTTPException(status_code=400, detail="结束时间不能早于开始时间")
    now = datetime.now(timezone.utc)
    rows, total = await alert_service.list_alerts(
        session, deviceId, level, status, page, pageSize,
        start=start_dt, end=end_dt, sla_state=sla, now=now, tags=tag,
    )
    config = await sla_service.get_or_create_config(session)
    # 先完成 ORM → 响应转换再提交：commit 会让实例过期，异步会话不能惰性加载
    items = [sla_service.alert_to_out(row, now, config) for row in rows]
    await session.commit()
    return ApiResponse(
        data=PageOut[AlertOut](items=items, total=total, page=page, page_size=pageSize)
    )


@router.post("/bulk-ack", response_model=ApiResponse[AlertBulkAckResult])
async def bulk_ack_alerts(
    body: AlertBulkAckRequest,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    """批量确认：一次请求完成多条，部分失败按类别返回，不整批 404。

    权限与单条确认一致（operator / admin）；viewer 由 require_role 直接 403。
    事务与审计与单条确认共用，避免 N 次请求与 N 条审计噪音。
    """
    operator = user.get("sub", "system")
    result = await alert_service.bulk_ack_alerts(
        session, body.alert_ids, operator=operator, note=body.note
    )
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_ALERT_ACK,
        target_type="alert",
        target_id=",".join(result["acked"][:5]),
        detail={
            "bulk": True,
            "requested": result["requested"],
            "acked": len(result["acked"]),
            "already_acked": len(result["already_acked"]),
            "not_found": len(result["not_found"]),
            "note_summary": _note_summary(body.note),
        },
    )
    await session.commit()
    return ApiResponse(data=AlertBulkAckResult(**result))


@router.post("/{alert_id}/ack", response_model=ApiResponse[AlertOut])
async def ack_alert(
    alert_id: str,
    body: Optional[AlertAckRequest] = None,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    """单条确认（v1.21.0）：可选 body `{"note": "..."}`；不传 body 的老客户端照常工作。"""
    operator = user.get("sub", "system")
    note = body.note if body else None
    alert = await alert_service.ack_alert(session, alert_id, operator=operator, note=note)
    if alert is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_ALERT_ACK,
        target_type="alert",
        target_id=alert_id,
        detail={
            "deviceId": alert.device_id,
            "level": alert.level,
            "metric": alert.metric,
            "note_summary": _note_summary(note),
        },
    )
    config = await sla_service.get_or_create_config(session)
    out = sla_service.alert_to_out(alert, datetime.now(timezone.utc), config)
    await session.commit()
    return ApiResponse(data=out)


@router.get("/summary", response_model=ApiResponse[AlertSummaryOut])
async def alert_summary(
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    data = await alert_service.alert_summary(session)
    return ApiResponse(data=AlertSummaryOut(**data))


@router.get("/sla-config", response_model=ApiResponse[SlaConfigOut])
async def get_sla_config(
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_user),
):
    """读 SLA 配置：登录即可查看，viewer 只读。"""
    config = await sla_service.get_or_create_config(session)
    out = sla_service.to_out(config)
    await session.commit()
    return ApiResponse(data=out)


@router.put("/sla-config", response_model=ApiResponse[SlaConfigOut])
async def update_sla_config(
    body: SlaConfigUpdate,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    """改 SLA 阈值与升级上限：仅 admin（与阈值/通知配置同一权限档）。

    修改只影响之后创建的告警；历史告警保留创建时的 sla_minutes 快照。
    """
    operator = user.get("sub", "system")
    config, changed = await sla_service.update_config(session, body, operator)
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_SLA_CONFIG_UPDATE,
        target_type="sla_config",
        target_id="1",
        detail={"changed": changed},
    )
    out = sla_service.to_out(config)
    await session.commit()
    return ApiResponse(data=out)


# 事件循环只持有 Task 的弱引用；显式保活，避免汇总通知任务被 GC 掉
_HANDOVER_TASKS: set = set()


def _schedule_handover_summary(**kwargs) -> None:
    task = asyncio.create_task(_dispatch_handover_summary(**kwargs))
    _HANDOVER_TASKS.add(task)
    task.add_done_callback(_HANDOVER_TASKS.discard)


async def _dispatch_handover_summary(**kwargs) -> None:
    """交接汇总通知的旁路任务：失败只记日志，不影响交接已落库的事实。"""
    try:
        await notify_service.send_handover_summary(**kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("值班交接汇总通知失败 to=%s", kwargs.get("to"))


@router.post("/handover", response_model=ApiResponse[AlertHandoverResult])
async def handover_alerts(
    body: AlertHandoverRequest,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_OPERATOR)),
):
    """值班交接：把未处理告警责任人批量交给下一班，落库 + 审计 + 一条汇总通知。

    权限与确认一致（operator / admin）；viewer 由 require_role 直接 403。
    交接**不重置** SLA 计时与升级计数：换班不改变告警真实响应时长，
    否则「先交接再交接」就能把超时洗成未超时。
    """
    operator = user.get("sub", "system")
    from_user = body.from_user or operator
    result = await sla_service.handover_open_alerts(
        session,
        from_user=from_user,
        to=body.to,
        include_unassigned=body.include_unassigned,
        note=body.note,
    )
    await audit_service.log(
        session,
        operator=operator,
        action=audit_service.ACTION_ALERT_HANDOVER,
        target_type="alert",
        target_id=body.to,
        detail={
            "from": from_user,
            "to": body.to,
            "count": result["count"],
            "window_start": result["window_start"].isoformat() if result["window_start"] else None,
            "window_end": result["window_end"].isoformat() if result["window_end"] else None,
            "include_unassigned": body.include_unassigned,
            "note_summary": _note_summary(body.note),
        },
    )
    await session.commit()

    notification = "skipped"
    if result["count"] > 0:
        notification = "queued"
        _schedule_handover_summary(
            to=body.to,
            from_user=from_user,
            count=result["count"],
            window_start=result["window_start"],
            window_end=result["window_end"],
            level=result["max_level"],
            operator=operator,
            note=body.note,
            # 旁路任务必须复用当前请求的数据库绑定，避免测试/多实例环境串库
            session_factory=async_sessionmaker(session.bind, expire_on_commit=False),
        )
    return ApiResponse(
        data=AlertHandoverResult(
            from_user=from_user,
            to=body.to,
            count=result["count"],
            window_start=result["window_start"],
            window_end=result["window_end"],
            notification=notification,
        )
    )
