"""维护窗口服务（v1.39.0）：范围匹配、抑制记录、结束汇总与巡检。"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import maintenance_window as maintenance, rule_conditions
from app.database import SessionFactory
from app.models import MaintenanceSummary, MaintenanceSuppression, MaintenanceWindow

logger = logging.getLogger(__name__)

SUMMARY_HOUR_LOOKBACK = 8 * 24  # 周期窗口巡检回看 8 天，覆盖跨天/跨周发生


def _window_id() -> str:
    return "mw-" + uuid.uuid4().hex[:12]


def to_out(row: MaintenanceWindow, now: Optional[datetime] = None) -> Dict[str, Any]:
    """列表/详情响应：状态与发生时间按当前时间推导，不依赖调度器。"""
    moment = now or datetime.now(timezone.utc)
    occurrence = maintenance.occurrence_for(row, moment)
    return {
        "window_id": row.window_id,
        "name": row.name,
        "reason": row.reason,
        "scope": row.scope,
        "mode": row.mode,
        "starts_at": maintenance.as_utc(row.starts_at),
        "ends_at": maintenance.as_utc(row.ends_at),
        "recurrence": row.recurrence,
        "status": row.status,
        "active": occurrence is not None,
        "current_start": occurrence.start if occurrence else None,
        "current_end": occurrence.end if occurrence else None,
        "next_start": maintenance.next_start(row, moment),
        "created_by": row.created_by,
        "created_at": maintenance.as_utc(row.created_at),
        "updated_at": maintenance.as_utc(row.updated_at),
    }


async def list_windows(session: AsyncSession) -> List[MaintenanceWindow]:
    stmt = select(MaintenanceWindow).order_by(MaintenanceWindow.id.desc())
    return list((await session.execute(stmt)).scalars().all())


async def get_window(session: AsyncSession, window_id: str) -> Optional[MaintenanceWindow]:
    stmt = select(MaintenanceWindow).where(MaintenanceWindow.window_id == window_id)
    return (await session.execute(stmt)).scalars().first()


async def create_window(
    session: AsyncSession,
    *,
    name: str,
    reason: Optional[str],
    scope: Any,
    mode: str,
    starts_at: Optional[datetime],
    ends_at: Optional[datetime],
    recurrence: Any,
    operator: str,
) -> MaintenanceWindow:
    normalized_scope = maintenance.normalize_scope(scope)
    if mode == maintenance.MODE_ONCE:
        start, end = maintenance.validate_once_times(starts_at, ends_at)
        normalized_recurrence = None
    elif mode == maintenance.MODE_WEEKLY:
        normalized_recurrence = maintenance.normalize_recurrence(recurrence)
        start = end = None
    else:
        raise maintenance.MaintenanceError("mode 只能是 once 或 weekly")

    row = MaintenanceWindow(
        window_id=_window_id(),
        name=name.strip(),
        reason=(reason or "").strip() or None,
        scope=normalized_scope,
        mode=mode,
        starts_at=start,
        ends_at=end,
        recurrence=normalized_recurrence,
        status=maintenance.STATUS_SCHEDULED,
        created_by=operator,
    )
    session.add(row)
    await session.flush()
    return row


async def update_window(
    session: AsyncSession,
    row: MaintenanceWindow,
    *,
    name: Optional[str] = None,
    reason: Optional[str] = None,
    scope: Any = None,
    scope_provided: bool = False,
    mode: Optional[str] = None,
    starts_at: Optional[datetime] = None,
    ends_at: Optional[datetime] = None,
    recurrence: Any = None,
    recurrence_provided: bool = False,
) -> Tuple[MaintenanceWindow, List[str]]:
    """更新窗口；返回实际变化的字段名（审计用）。"""
    changed: List[str] = []
    if row.status == maintenance.STATUS_ENDED:
        raise maintenance.MaintenanceError("窗口已结束，不能再修改")

    if name is not None and name.strip() != row.name:
        row.name = name.strip()
        changed.append("name")
    if reason is not None:
        cleaned = reason.strip() or None
        if cleaned != row.reason:
            row.reason = cleaned
            changed.append("reason")
    if scope_provided:
        normalized = maintenance.normalize_scope(scope)
        if rule_conditions.signature(normalized) != rule_conditions.signature(row.scope):
            row.scope = normalized
            changed.append("scope")
    if mode is not None and mode != row.mode:
        row.mode = mode
        changed.append("mode")

    target_mode = row.mode
    if target_mode == maintenance.MODE_ONCE:
        new_start = starts_at if starts_at is not None else row.starts_at
        new_end = ends_at if ends_at is not None else row.ends_at
        start, end = maintenance.validate_once_times(new_start, new_end)
        if maintenance.as_utc(row.starts_at) != start:
            row.starts_at = start
            changed.append("starts_at")
        if maintenance.as_utc(row.ends_at) != end:
            row.ends_at = end
            changed.append("ends_at")
        if row.recurrence is not None:
            row.recurrence = None
            changed.append("recurrence")
    else:
        raw_recurrence = recurrence if recurrence_provided else row.recurrence
        normalized = maintenance.normalize_recurrence(raw_recurrence)
        if normalized != (row.recurrence or {}):
            row.recurrence = normalized
            changed.append("recurrence")
        if row.starts_at is not None or row.ends_at is not None:
            row.starts_at = None
            row.ends_at = None
            changed.append("starts_at")
            changed.append("ends_at")

    await session.flush()
    return row, sorted(set(changed))


async def end_window(
    session: AsyncSession,
    row: MaintenanceWindow,
    *,
    now: Optional[datetime] = None,
    operator: str = "system",
) -> Optional[MaintenanceSummary]:
    """手工提前结束：立即停止匹配，并为正在进行的一次发生生成汇总。

    已抑制的历史记录保留，不回填成告警。
    """
    moment = maintenance.as_utc(now) or datetime.now(timezone.utc)
    if row.status == maintenance.STATUS_ENDED:
        return None

    occurrence = maintenance.occurrence_for(row, moment)
    row.status = maintenance.STATUS_ENDED
    await session.flush()

    if occurrence is not None:
        return await ensure_summary(
            session,
            row,
            occurrence_start=occurrence.start,
            occurrence_end=moment,
            ended_early=True,
        )
    return None


async def active_window(
    session: AsyncSession,
    *,
    device_id: str,
    group_name: Optional[str],
    tag_names: Optional[Iterable[str]],
    now: Optional[datetime] = None,
) -> Tuple[Optional[MaintenanceWindow], Optional[datetime]]:
    """返回命中的维护窗口与本次发生开始时间（多个重叠时取最早创建的一条）。"""
    moment = maintenance.as_utc(now) or datetime.now(timezone.utc)
    rows = list(
        (
            await session.execute(
                select(MaintenanceWindow)
                .where(MaintenanceWindow.status == maintenance.STATUS_SCHEDULED)
                .order_by(MaintenanceWindow.id.asc())
            )
        ).scalars().all()
    )
    tags = list(tag_names or [])
    for row in rows:
        occurrence = maintenance.occurrence_for(row, moment)
        if occurrence is None:
            continue
        if row.scope is not None:
            try:
                matched = rule_conditions.matches(
                    row.scope, device_id=device_id, group_name=group_name, tag_names=tags
                )
            except Exception:  # pragma: no cover - 结构异常时 fail-closed
                logger.exception("维护窗口范围求值失败 window=%s", row.window_id)
                matched = False
            if not matched:
                continue
        return row, occurrence.start
    return None, None


async def record_suppressions(
    session: AsyncSession,
    window: MaintenanceWindow,
    occurrence_start: datetime,
    device_id: str,
    hits: List[Dict[str, Any]],
) -> int:
    """记录「本会触发告警但被窗口静音」的上报；不创建 DeviceAlert。"""
    count = 0
    for hit in hits:
        session.add(
            MaintenanceSuppression(
                window_id=window.window_id,
                occurrence_start=occurrence_start,
                device_id=device_id,
                metric=hit["metric"],
                level=hit["level"],
                value=hit["value"],
                threshold=hit["threshold"],
                rule_name=hit.get("rule_name"),
            )
        )
        count += 1
    if count:
        await session.flush()
    return count


async def ensure_summary(
    session: AsyncSession,
    window: MaintenanceWindow,
    *,
    occurrence_start: datetime,
    occurrence_end: datetime,
    ended_early: bool = False,
) -> Optional[MaintenanceSummary]:
    """生成（幂等）一次窗口发生的汇总；已存在则直接返回。"""
    existing = (
        await session.execute(
            select(MaintenanceSummary).where(
                MaintenanceSummary.window_id == window.window_id,
                MaintenanceSummary.occurrence_start == occurrence_start,
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing

    rows = list(
        (
            await session.execute(
                select(MaintenanceSuppression).where(
                    MaintenanceSuppression.window_id == window.window_id,
                    MaintenanceSuppression.occurrence_start == occurrence_start,
                )
            )
        ).scalars().all()
    )
    devices = sorted({row.device_id for row in rows})
    metrics: Dict[str, int] = {}
    levels: Dict[str, int] = {}
    for row in rows:
        metrics[row.metric] = metrics.get(row.metric, 0) + 1
        levels[row.level] = levels.get(row.level, 0) + 1

    summary = MaintenanceSummary(
        window_id=window.window_id,
        occurrence_start=occurrence_start,
        occurrence_end=occurrence_end,
        suppressed_count=len(rows),
        device_count=len(devices),
        critical_suppressed=levels.get("critical", 0),
        devices=devices,
        metrics=metrics,
        levels=levels,
        ended_early=ended_early,
    )
    session.add(summary)
    await session.flush()
    logger.info(
        "维护窗口汇总 window=%s occurrence=%s suppressed=%d critical=%d devices=%d",
        window.window_id, occurrence_start.isoformat(), len(rows),
        summary.critical_suppressed, len(devices),
    )
    return summary


def _occurrences_between(window: MaintenanceWindow, start: datetime, end: datetime) -> List[Tuple[datetime, datetime]]:
    """枚举 [start, end) 内已结束的窗口发生（once 单次，weekly 按天对齐）。"""
    results: List[Tuple[datetime, datetime]] = []
    if window.mode == maintenance.MODE_ONCE:
        s = maintenance.as_utc(window.starts_at)
        e = maintenance.as_utc(window.ends_at)
        if s and e and s < end and e <= end:
            results.append((s, e))
        return results

    recurrence = window.recurrence or {}
    try:
        clock = maintenance._parse_hhmm(recurrence.get("start"))
        weekday = int(recurrence["weekday"])
        duration = int(recurrence["duration_minutes"])
    except (KeyError, TypeError, ValueError):
        return results

    day = start.date()
    while day <= end.date():
        if day.weekday() == weekday:
            s = maintenance._utc_datetime(day, clock)
            e = s + timedelta(minutes=duration)
            if s < end and e <= end:
                results.append((s, e))
        day += timedelta(days=1)
    return results


async def sweep_windows(
    session_factory=None,
    now: Optional[datetime] = None,
) -> int:
    """巡检：为已结束的窗口发生补汇总，并把一次性窗口标记为 ended。

    返回本轮新生成的汇总条数。窗口匹配不依赖本巡检（按时间实时推导），
    因此调度器短暂停摆不会导致「窗口失效」；这里只负责收尾与汇总。
    """
    factory = session_factory or SessionFactory
    moment = maintenance.as_utc(now) or datetime.now(timezone.utc)
    lookback = moment - timedelta(hours=SUMMARY_HOUR_LOOKBACK)
    created = 0

    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(MaintenanceWindow).where(
                        MaintenanceWindow.status == maintenance.STATUS_SCHEDULED
                    )
                )
            ).scalars().all()
        )
        for row in rows:
            window_start = maintenance.as_utc(row.created_at) or lookback
            scan_from = max(window_start, lookback)
            for occurrence_start, occurrence_end in _occurrences_between(row, scan_from, moment):
                summary = await ensure_summary(
                    session,
                    row,
                    occurrence_start=occurrence_start,
                    occurrence_end=occurrence_end,
                )
                if summary is not None:
                    created += 1
            if row.mode == maintenance.MODE_ONCE:
                end = maintenance.as_utc(row.ends_at)
                if end and end <= moment:
                    row.status = maintenance.STATUS_ENDED
        await session.commit()

    return created


async def list_summaries(
    session: AsyncSession, window_id: str, limit: int = 20
) -> List[MaintenanceSummary]:
    stmt = (
        select(MaintenanceSummary)
        .where(MaintenanceSummary.window_id == window_id)
        .order_by(MaintenanceSummary.occurrence_start.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_suppressions(
    session: AsyncSession,
    window_id: str,
    *,
    page: int,
    page_size: int,
) -> Tuple[List[MaintenanceSuppression], int]:
    from sqlalchemy import func

    stmt = select(MaintenanceSuppression).where(MaintenanceSuppression.window_id == window_id)
    count_stmt = select(func.count()).select_from(MaintenanceSuppression).where(
        MaintenanceSuppression.window_id == window_id
    )
    stmt = stmt.order_by(MaintenanceSuppression.created_at.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total
