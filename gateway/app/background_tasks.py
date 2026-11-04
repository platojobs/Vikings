"""后台定时任务：设备离线巡检与数据保留清理。

IoT 真实场景的三个必备能力：
1. 离线巡检：LWT 能覆盖"异常掉线"，但设备"温和下线后再也没上来"或网络分区
   期间 Broker 未及时触发遗嘱的情况，需要二级兜底——超过阈值无上报即强制置离线。
2. 时序保留：sensor_record 长期无限增长会拖垮查询与磁盘，按天滚动删除（默认 90 天）。
3. 告警/审计保留：v1.19.0 起补上生命周期策略（默认 acked 告警 180 天、审计 365 天）。

清理口径：
- sensor_record：按 `ts` 删除（设备上报时间），与展示口径一致；
- device_alert：**只删已确认（acked）**且超过保留期的记录；`open` 告警永不自动删除，
  避免把未处理的运维欠账悄悄清掉；
- audit_log：按 `created_at` 删除，保留期更长（默认 365 天），审计记录不可重建。

所有清理都分块提交，避免大事务锁表；`purge_expired_data` 可被定时器与
`python -m app.maintenance purge` 手动调用，两者走同一实现。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional

from sqlalchemy import delete, func, select, update

from app.config import get_settings
from app.database import SessionFactory
from app.models import AuditLog, Device, DeviceAlert, SensorRecord
from app.topics import DEVICE_STATUS_OFFLINE

logger = logging.getLogger(__name__)
settings = get_settings()

SessionFactoryLike = Callable[[], object]
CHUNK_SIZE = 5000


def _factory(session_factory: Optional[SessionFactoryLike]):
    return session_factory or SessionFactory


def _cutoff(days: int, now: Optional[datetime]) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


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


async def _purge_where(
    model,
    condition,
    *,
    session_factory: Optional[SessionFactoryLike] = None,
    chunk_size: int = CHUNK_SIZE,
) -> int:
    """按条件分块删除，返回删除行数；每块独立提交，避免长事务。"""
    deleted = 0
    factory = _factory(session_factory)
    async with factory() as session:
        while True:
            subq = select(model.id).where(condition).limit(chunk_size)
            stmt = delete(model).where(model.id.in_(subq))
            result = await session.execute(stmt)
            await session.commit()
            n = int(result.rowcount or 0)
            deleted += n
            if n < chunk_size:
                break
    return deleted


async def _count_where(
    model,
    condition,
    *,
    session_factory: Optional[SessionFactoryLike] = None,
) -> int:
    factory = _factory(session_factory)
    async with factory() as session:
        stmt = select(func.count()).select_from(model).where(condition)
        return int((await session.execute(stmt)).scalar_one())


async def purge_old_records(
    *,
    session_factory: Optional[SessionFactoryLike] = None,
    now: Optional[datetime] = None,
) -> int:
    """删除超过保留期的传感器时序数据（默认 90 天，按上报时间 `ts`）。"""
    cutoff = _cutoff(settings.data_retention_days, now)
    deleted = await _purge_where(
        SensorRecord, SensorRecord.ts < cutoff, session_factory=session_factory
    )
    if deleted:
        logger.info(
            "数据保留清理：删除 %d 条早于 %s 的传感器记录（保留 %d 天）",
            deleted,
            cutoff.isoformat(),
            settings.data_retention_days,
        )
    return deleted


async def purge_acked_alerts(
    *,
    session_factory: Optional[SessionFactoryLike] = None,
    now: Optional[datetime] = None,
) -> int:
    """删除已确认且超过保留期的告警（默认 180 天）。

    只处理 `status='acked'`：未确认告警代表尚未完成的运维动作，
    无论多旧都不自动删除。
    """
    cutoff = _cutoff(settings.alert_retention_days, now)
    deleted = await _purge_where(
        DeviceAlert,
        (DeviceAlert.status == "acked") & (DeviceAlert.created_at < cutoff),
        session_factory=session_factory,
    )
    if deleted:
        logger.info(
            "告警保留清理：删除 %d 条早于 %s 的已确认告警（保留 %d 天）",
            deleted,
            cutoff.isoformat(),
            settings.alert_retention_days,
        )
    return deleted


async def purge_audit_logs(
    *,
    session_factory: Optional[SessionFactoryLike] = None,
    now: Optional[datetime] = None,
) -> int:
    """删除超过保留期的审计日志（默认 365 天）。

    审计日志口径独立于告警：它是操作追溯依据，保留期更长；
    如需合规归档，应在删除前导出（见 docs/operations.md）。
    """
    cutoff = _cutoff(settings.audit_retention_days, now)
    deleted = await _purge_where(
        AuditLog, AuditLog.created_at < cutoff, session_factory=session_factory
    )
    if deleted:
        logger.info(
            "审计保留清理：删除 %d 条早于 %s 的审计日志（保留 %d 天）",
            deleted,
            cutoff.isoformat(),
            settings.audit_retention_days,
        )
    return deleted


async def purge_expired_data(
    *,
    session_factory: Optional[SessionFactoryLike] = None,
    now: Optional[datetime] = None,
) -> Dict[str, int]:
    """一次性执行三类清理，返回各类删除行数（供定时器与运维 CLI 复用）。"""
    return {
        "records": await purge_old_records(session_factory=session_factory, now=now),
        "alerts": await purge_acked_alerts(session_factory=session_factory, now=now),
        "audit_logs": await purge_audit_logs(session_factory=session_factory, now=now),
    }


async def count_expired_data(
    *,
    session_factory: Optional[SessionFactoryLike] = None,
    now: Optional[datetime] = None,
) -> Dict[str, int]:
    """只统计不删除，供 `--dry-run` 预检。"""
    records_cutoff = _cutoff(settings.data_retention_days, now)
    alerts_cutoff = _cutoff(settings.alert_retention_days, now)
    audit_cutoff = _cutoff(settings.audit_retention_days, now)
    return {
        "records": await _count_where(
            SensorRecord, SensorRecord.ts < records_cutoff, session_factory=session_factory
        ),
        "alerts": await _count_where(
            DeviceAlert,
            (DeviceAlert.status == "acked") & (DeviceAlert.created_at < alerts_cutoff),
            session_factory=session_factory,
        ),
        "audit_logs": await _count_where(
            AuditLog, AuditLog.created_at < audit_cutoff, session_factory=session_factory
        ),
    }


def func_now():
    """数据库当前时间（与列定义的 server_default=func.now() 保持一致）。"""
    return func.now()
