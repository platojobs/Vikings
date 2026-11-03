"""后台定时任务：设备离线巡检与时序数据保留清理。

IoT 真实场景的两个必备能力：
1. 离线巡检：LWT 能覆盖"异常掉线"，但设备"温和下线后再也没上来"或网络分区
   期间 Broker 未及时触发遗嘱的情况，需要二级兜底——超过阈值无上报即强制置离线。
2. 数据保留：sensor_record 是时序数据，长期无限增长会拖垮查询与磁盘。
   按天数滚动删除，演示环境默认保留 90 天，生产可按需放大。
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from app.config import get_settings
from app.database import SessionFactory
from app.models import Device, SensorRecord
from app.topics import DEVICE_STATUS_OFFLINE

logger = logging.getLogger(__name__)
settings = get_settings()


async def sweep_offline_devices() -> int:
    """将超过阈值未上报的设备强制置为离线。

    返回被置为离线的设备数。这个巡检是 LWT 之外的二级兜底，
    不依赖 Broker 的遗嘱触发，保证网络分区等极端场景下状态最终一致。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.device_offline_timeout_seconds)
    async with SessionFactory() as session:
        stmt = (
            update(Device)
            .where(Device.status == "online", Device.last_report_at < cutoff)
            .values(status=DEVICE_STATUS_OFFLINE, updated_at=func_now())
        )
        result = await session.execute(stmt)
        await session.commit()
        count = int(result.rowcount or 0)
        if count:
            logger.info("离线巡检：%d 台设备因超时未上报被置为离线", count)
        return count


async def purge_old_records() -> int:
    """删除超过保留期的传感器时序数据。

    返回删除行数。用 delete + commit 分块执行，避免大事务锁表。
    生产环境建议按时间分区或引入 TimescaleDB 的 drop_chunks。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.data_retention_days)
    deleted = 0
    chunk_size = 5000
    async with SessionFactory() as session:
        while True:
            subq = (
                select(SensorRecord.id)
                .where(SensorRecord.ts < cutoff)
                .limit(chunk_size)
            )
            stmt = delete(SensorRecord).where(SensorRecord.id.in_(subq))
            result = await session.execute(stmt)
            await session.commit()
            n = int(result.rowcount or 0)
            deleted += n
            if n < chunk_size:
                break
    if deleted:
        logger.info("数据保留清理：删除 %d 条早于 %s 的传感器记录", deleted, cutoff.isoformat())
    return deleted


def func_now():
    """数据库当前时间（与列定义的 server_default=func.now() 保持一致）。"""
    from sqlalchemy import func
    return func.now()
