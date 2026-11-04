"""设备服务：元数据维护、状态更新、时序数据写入与查询。

所有函数都接收外部传入的 AsyncSession，事务边界由调用方（消息处理器 / HTTP 路由）控制。
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceAlert, SensorRecord
from app.schemas import DeviceStatusMessage, SensorReportMessage
from app.topics import DEVICE_STATUS_OFFLINE, DEVICE_STATUS_ONLINE

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_device(session: AsyncSession, device_id: str) -> Device:
    """设备首次上报时自动建档，避免因为漏登记导致数据丢失。

    量产环境通常由出厂导入建档，这里的自动建档是兜底策略。
    """
    device = await session.get(Device, device_id)
    if device is None:
        device = Device(
            device_id=device_id,
            name="设备 " + device_id,
            model="VG-S100",
            firmware_version="1.0.0",
            status=DEVICE_STATUS_OFFLINE,
        )
        session.add(device)
        await session.flush()
        logger.info("自动建档新设备: %s", device_id)
    return device


async def apply_status(session: AsyncSession, msg: DeviceStatusMessage) -> Device:
    """处理上下线消息：状态 + 固件版本 + 时间戳。"""
    device = await ensure_device(session, msg.device_id)
    device.status = msg.status
    if msg.firmware_version:
        device.firmware_version = msg.firmware_version
    if msg.status == DEVICE_STATUS_ONLINE:
        device.last_online_at = msg.ts
    await session.flush()
    return device


async def save_report(session: AsyncSession, msg: SensorReportMessage) -> bool:
    """写入一条传感器数据。

    返回 True 表示真正写入，False 表示重复消息被幂等丢弃。
    MQTT QoS1 在弱网下会重复投递，这里用 (device_id, seq, ts) 唯一约束兜底。
    """
    record = SensorRecord(
        device_id=msg.device_id,
        temperature=msg.temperature,
        humidity=msg.humidity,
        voltage=msg.voltage,
        rssi=msg.rssi,
        battery=msg.battery,
        seq=msg.seq,
        ts=msg.ts,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        logger.debug("重复上报已忽略: device=%s seq=%s", msg.device_id, msg.seq)
        return False
    return True


async def touch_report_time(session: AsyncSession, msg: SensorReportMessage) -> None:
    """上报即视为在线：解决设备未发 retained 状态时的状态兜底。"""
    device = await ensure_device(session, msg.device_id)
    device.last_report_at = msg.ts
    if device.status != DEVICE_STATUS_ONLINE and msg.ts.date() == _now().date():
        device.status = DEVICE_STATUS_ONLINE
        device.last_online_at = msg.ts
    await session.flush()


async def list_devices(
    session: AsyncSession,
    status: Optional[str],
    keyword: Optional[str],
    page: int,
    page_size: int,
    group_name: Optional[str] = None,
    has_alert: bool = False,
) -> Tuple[List[Device], int]:
    stmt = select(Device)
    count_stmt = select(func.count()).select_from(Device)

    if has_alert:
        # 「有告警」在数据库层按未处理告警聚合，不受告警列表分页大小影响
        alert_device_ids = (
            select(DeviceAlert.device_id)
            .where(DeviceAlert.status == "open")
            .distinct()
        )
        stmt = stmt.where(Device.device_id.in_(alert_device_ids))
        count_stmt = count_stmt.where(Device.device_id.in_(alert_device_ids))
    if status:
        stmt = stmt.where(Device.status == status)
        count_stmt = count_stmt.where(Device.status == status)
    if group_name:
        stmt = stmt.where(Device.group_name == group_name)
        count_stmt = count_stmt.where(Device.group_name == group_name)
    if keyword:
        like = "%" + keyword + "%"
        cond = Device.device_id.ilike(like) | Device.name.ilike(like)
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    stmt = stmt.order_by(Device.last_report_at.desc().nullslast(), Device.device_id)
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    total = (await session.execute(count_stmt)).scalar_one()
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows), int(total)


async def create_device(
    session: AsyncSession,
    device_id: str,
    name: str,
    model: str,
    firmware_version: str,
    group_name: str,
) -> Tuple[Optional[Device], str]:
    """手工注册设备；重复 ID 返回错误文案，由路由转 409。"""
    if await session.get(Device, device_id) is not None:
        return None, "设备 ID 已存在"
    device = Device(
        device_id=device_id,
        name=name,
        model=model,
        firmware_version=firmware_version,
        group_name=group_name,
        status=DEVICE_STATUS_OFFLINE,
    )
    session.add(device)
    await session.flush()
    return device, ""


async def bulk_create_devices(
    session: AsyncSession, items: List[dict]
) -> Tuple[List[str], List[str]]:
    """批量注册：一次性查询已有 ID，重复项跳过而不整批失败。"""
    if not items:
        return [], []

    ids = [item["device_id"] for item in items]
    existing = set(
        (await session.execute(select(Device.device_id).where(Device.device_id.in_(ids))))
        .scalars()
        .all()
    )

    created: List[str] = []
    skipped: List[str] = []
    seen: set = set()
    for item in items:
        device_id = item["device_id"]
        if device_id in existing or device_id in seen:
            skipped.append(device_id)
            continue
        seen.add(device_id)
        session.add(Device(status=DEVICE_STATUS_OFFLINE, **item))
        created.append(device_id)

    await session.flush()
    return created, skipped


async def update_device(
    session: AsyncSession,
    device_id: str,
    changes: dict,
) -> Tuple[Optional[Device], str]:
    """编辑设备元数据：只覆盖传入的字段，状态不由后台直接修改。"""
    device = await session.get(Device, device_id)
    if device is None:
        return None, "设备不存在"
    for key, value in changes.items():
        if value is not None:
            setattr(device, key, value)
    await session.flush()
    return device, ""


async def delete_device(session: AsyncSession, device_id: str) -> Tuple[bool, str]:
    """删除设备：存在时序数据时拒绝，避免产生孤儿数据。"""
    device = await session.get(Device, device_id)
    if device is None:
        return False, "设备不存在"

    record_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(SensorRecord)
                .where(SensorRecord.device_id == device_id)
            )
        ).scalar_one()
    )
    if record_count > 0:
        return False, f"设备存在 {record_count} 条历史数据，请先清理后再删除"

    await session.delete(device)
    await session.flush()
    return True, ""


async def list_groups(session: AsyncSession) -> List[dict]:
    """分组概览：每组设备数与在线/离线数量，用于列表筛选与拓扑概览。"""
    stmt = (
        select(
            Device.group_name.label("group_name"),
            func.count().label("total"),
            func.sum(case((Device.status == DEVICE_STATUS_ONLINE, 1), else_=0)).label("online"),
        )
        .group_by(Device.group_name)
        .order_by(Device.group_name)
    )
    rows = (await session.execute(stmt)).all()
    result = []
    for row in rows:
        total = int(row.total)
        online = int(row.online or 0)
        result.append(
            {
                "group_name": row.group_name,
                "total": total,
                "online": online,
                "offline": total - online,
            }
        )
    return result


async def get_device(session: AsyncSession, device_id: str) -> Optional[Device]:
    return await session.get(Device, device_id)


async def latest_record(session: AsyncSession, device_id: str) -> Optional[SensorRecord]:
    stmt = (
        select(SensorRecord)
        .where(SensorRecord.device_id == device_id)
        .order_by(SensorRecord.ts.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def latest_records_all(
    session: AsyncSession,
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    device_ids: Optional[List[str]] = None,
) -> List[SensorRecord]:
    """全局每个设备的最新一条（仪表盘首屏兜底，避免前端空窗）。

    过滤与分页均为可选：不传时与历史行为完全一致（无过滤、无截断、按时间倒序）。
    设备状态 / 关键词过滤需要关联设备表，仅在传入时才做 join；
    device_ids 用于移动看板只取当前已加载页设备的实时值，避免全量拉取。
    """
    subq = (
        select(SensorRecord.device_id, func.max(SensorRecord.ts).label("max_ts"))
        .group_by(SensorRecord.device_id)
        .subquery()
    )
    stmt = select(SensorRecord).join(
        subq, (SensorRecord.device_id == subq.c.device_id) & (SensorRecord.ts == subq.c.max_ts)
    )

    if device_ids is not None:
        if not device_ids:
            return []
        stmt = stmt.where(SensorRecord.device_id.in_(device_ids))

    if status or keyword:
        stmt = stmt.join(Device, Device.device_id == SensorRecord.device_id)
        if status:
            stmt = stmt.where(Device.status == status)
        if keyword:
            like = "%" + keyword + "%"
            stmt = stmt.where(Device.device_id.ilike(like) | Device.name.ilike(like))

    stmt = stmt.order_by(SensorRecord.ts.desc())
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def query_records(
    session: AsyncSession,
    device_id: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
    limit: int,
) -> List[SensorRecord]:
    stmt = select(SensorRecord)
    if device_id:
        stmt = stmt.where(SensorRecord.device_id == device_id)
    if start:
        stmt = stmt.where(SensorRecord.ts >= start)
    if end:
        stmt = stmt.where(SensorRecord.ts <= end)
    stmt = stmt.order_by(SensorRecord.ts.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    # 图表需要时间正序，统一在服务层翻转，避免前端各自处理
    return list(reversed(rows))


async def count_report_today(session: AsyncSession) -> int:
    today_start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = select(func.count()).select_from(SensorRecord).where(SensorRecord.ts >= today_start)
    return int((await session.execute(stmt)).scalar_one())


async def aggregate_records(
    session: AsyncSession,
    device_id: str,
    bucket: str,
    start: Optional[datetime],
    end: Optional[datetime],
) -> List[dict]:
    """按时间桶聚合时序数据：每桶输出三指标的 avg/min/max。

    bucket: hour / day。分桶表达式按数据库方言切换：
    PostgreSQL 用 date_trunc，SQLite 用 strftime（二者返回值都按字符串排序等价）。
    """
    from app.config import get_settings

    if get_settings().is_sqlite:
        fmt = "%Y-%m-%d %H:00:00" if bucket == "hour" else "%Y-%m-%d"
        bucket_expr = func.strftime(fmt, SensorRecord.ts)
    else:
        bucket_expr = func.date_trunc(bucket, SensorRecord.ts)

    stmt = select(
        bucket_expr.label("bucket_start"),
        func.count().label("count"),
        func.avg(SensorRecord.temperature).label("temperature_avg"),
        func.min(SensorRecord.temperature).label("temperature_min"),
        func.max(SensorRecord.temperature).label("temperature_max"),
        func.avg(SensorRecord.humidity).label("humidity_avg"),
        func.min(SensorRecord.humidity).label("humidity_min"),
        func.max(SensorRecord.humidity).label("humidity_max"),
        func.avg(SensorRecord.voltage).label("voltage_avg"),
        func.min(SensorRecord.voltage).label("voltage_min"),
        func.max(SensorRecord.voltage).label("voltage_max"),
    ).where(SensorRecord.device_id == device_id)

    if start:
        stmt = stmt.where(SensorRecord.ts >= start)
    if end:
        stmt = stmt.where(SensorRecord.ts <= end)

    stmt = stmt.group_by(bucket_expr).order_by(bucket_expr)
    rows = (await session.execute(stmt)).all()

    result = []
    for row in rows:
        item = dict(row._mapping)
        # SQLite strftime 返回字符串，PostgreSQL date_trunc 返回 datetime，统一转 datetime
        bs = item["bucket_start"]
        if isinstance(bs, str):
            fmt = "%Y-%m-%d %H:%M:%S" if bucket == "hour" else "%Y-%m-%d"
            item["bucket_start"] = datetime.strptime(bs, fmt).replace(tzinfo=timezone.utc)
        for key in ("temperature_avg", "humidity_avg", "voltage_avg"):
            item[key] = round(float(item[key]), 2)
        for key in (
            "temperature_min", "temperature_max",
            "humidity_min", "humidity_max",
            "voltage_min", "voltage_max",
        ):
            item[key] = float(item[key])
        result.append(item)
    return result
