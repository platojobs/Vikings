"""统计服务：Web 指标卡与移动端概览页共用同一套口径。

v1.27.0 在既有 `GET /api/stats/overview` 上追加概览字段（凭证缺口、告警级别分布、
24 小时趋势、异常设备），不新增第二套统计端点，避免 Web 与移动端出现两套定义。
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceAlert, SensorRecord
from app.schemas import AbnormalDeviceOut, StatsOverviewOut, TrendPointOut
from app.services.alert_service import count_open_alerts
from app.services.device_service import count_report_today
from app.topics import DEVICE_STATUS_ONLINE

# 概览趋势窗口：当前小时 + 之前 23 个小时
TREND_HOURS = 24
# 异常设备列表上限：概览页只做「需要处理」的提示，完整清单在设备和告警页面
ABNORMAL_LIMIT = 10


def _bucket_expr(session: AsyncSession, column):
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return func.strftime("%Y-%m-%d %H:00:00", column)
    return func.date_trunc("hour", column)


def _normalize_hour(value) -> Optional[datetime]:
    """把 date_trunc 的 timestamp / strftime 的字符串统一为 UTC 小时起点。"""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            parsed = datetime.fromisoformat(value.replace(" ", "T"))
        return parsed.replace(tzinfo=timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def _normalize_time(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        return _normalize_hour(value)
    return None


async def _hourly_counts(
    session: AsyncSession, column, window_start: datetime
) -> Dict[datetime, int]:
    expr = _bucket_expr(session, column)
    rows = (
        await session.execute(
            select(expr, func.count()).where(column >= window_start).group_by(expr)
        )
    ).all()
    counts: Dict[datetime, int] = {}
    for bucket, count in rows:
        hour = _normalize_hour(bucket)
        if hour is not None:
            counts[hour] = counts.get(hour, 0) + int(count)
    return counts


async def _trend(
    session: AsyncSession, now: datetime
) -> tuple[List[TrendPointOut], bool, Optional[datetime]]:
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    window_start = current_hour - timedelta(hours=TREND_HOURS - 1)

    reports = await _hourly_counts(session, SensorRecord.ts, window_start)
    alerts = await _hourly_counts(session, DeviceAlert.created_at, window_start)

    points: List[TrendPointOut] = []
    for offset in range(TREND_HOURS):
        hour = window_start + timedelta(hours=offset)
        points.append(
            TrendPointOut(
                hour_start=hour,
                reports=reports.get(hour, 0),
                alerts=alerts.get(hour, 0),
            )
        )

    earliest = (
        await session.execute(select(func.min(SensorRecord.ts)))
    ).scalar_one()
    first_record_at = _normalize_time(earliest)
    # 只有窗口起点之前就有数据，才说明 24 小时窗口被完整覆盖
    trend_complete = first_record_at is not None and first_record_at < window_start
    return points, trend_complete, first_record_at


async def _abnormal_devices(session: AsyncSession) -> List[AbnormalDeviceOut]:
    alert_rows = (
        await session.execute(
            select(DeviceAlert.device_id, func.count())
            .where(DeviceAlert.status == "open")
            .group_by(DeviceAlert.device_id)
        )
    ).all()
    counts = {device_id: int(count) for device_id, count in alert_rows}
    alert_device_ids = list(counts)

    conditions = [Device.status != DEVICE_STATUS_ONLINE]
    if alert_device_ids:
        conditions.append(Device.device_id.in_(alert_device_ids))
    devices = (
        (await session.execute(select(Device).where(or_(*conditions)))).scalars().all()
    )

    items: List[AbnormalDeviceOut] = []
    for device in devices:
        count = counts.get(device.device_id, 0)
        offline = device.status != DEVICE_STATUS_ONLINE
        if count and offline:
            reason = "alert+offline"
        elif count:
            reason = "alert"
        else:
            reason = "offline"
        items.append(
            AbnormalDeviceOut(
                device_id=device.device_id,
                name=device.name,
                status=device.status,
                open_alert_count=count,
                reason=reason,
            )
        )

    # 未处理告警多的优先；同为有告警时离线设备更值得先看；最后按设备 ID 稳定排序
    items.sort(
        key=lambda item: (
            -item.open_alert_count,
            item.status == DEVICE_STATUS_ONLINE,
            item.device_id,
        )
    )
    return items[:ABNORMAL_LIMIT]


async def overview(session: AsyncSession) -> StatsOverviewOut:
    total = int(
        (await session.execute(select(func.count()).select_from(Device))).scalar_one()
    )
    online = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Device)
                .where(Device.status == DEVICE_STATUS_ONLINE)
            )
        ).scalar_one()
    )
    credential_missing = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Device)
                .where(Device.mqtt_credential_issued_at.is_(None))
            )
        ).scalar_one()
    )
    level_rows = (
        await session.execute(
            select(DeviceAlert.level, func.count())
            .where(DeviceAlert.status == "open")
            .group_by(DeviceAlert.level)
        )
    ).all()
    levels = {level: int(count) for level, count in level_rows}

    now = datetime.now(timezone.utc)
    trend, trend_complete, first_record_at = await _trend(session, now)

    return StatsOverviewOut(
        device_total=total,
        device_online=online,
        device_offline=total - online,
        report_today=await count_report_today(session),
        alert_open=await count_open_alerts(session),
        device_credential_missing=credential_missing,
        alert_warning_open=levels.get("warning", 0),
        alert_critical_open=levels.get("critical", 0),
        trend=trend,
        trend_complete=trend_complete,
        first_record_at=first_record_at,
        abnormal_devices=await _abnormal_devices(session),
    )
