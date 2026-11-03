"""设备服务：元数据维护、状态更新、时序数据写入与查询。

所有函数都接收外部传入的 AsyncSession，事务边界由调用方（消息处理器 / HTTP 路由）控制。
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, SensorRecord
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
) -> Tuple[List[Device], int]:
    stmt = select(Device)
    count_stmt = select(func.count()).select_from(Device)

    if status:
        stmt = stmt.where(Device.status == status)
        count_stmt = count_stmt.where(Device.status == status)
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


async def latest_records_all(session: AsyncSession) -> List[SensorRecord]:
    """全局每个设备的最新一条（仪表盘首屏兜底，避免前端空窗）。"""
    subq = (
        select(SensorRecord.device_id, func.max(SensorRecord.ts).label("max_ts"))
        .group_by(SensorRecord.device_id)
        .subquery()
    )
    stmt = (
        select(SensorRecord)
        .join(subq, (SensorRecord.device_id == subq.c.device_id) & (SensorRecord.ts == subq.c.max_ts))
        .order_by(SensorRecord.ts.desc())
    )
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
