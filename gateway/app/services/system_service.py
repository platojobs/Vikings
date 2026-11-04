"""系统状态服务：面向管理后台的只读运维快照。

聚合网关进程、MQTT 连接、存储容量与后台任务参数四类信息；
管理后台「系统状态」页据此判断故障落在网关、Broker 还是数据库，
不必再登录服务器查日志或翻 .env。

安全约束：只回传脱敏后的 Webhook 目标地址，绝不暴露 token、
JWT 密钥、LLM Key 等敏感配置。
"""

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Device, DeviceAlert, SensorRecord
from app.schemas import (
    GatewayStatusOut,
    MqttStatusOut,
    NotifyStatusOut,
    SchedulerStatusOut,
    StorageStatusOut,
    SystemStatusOut,
)
from app.topics import DEVICE_STATUS_ONLINE

settings = get_settings()


def mask_webhook_target(url: str) -> str:
    """把 Webhook 地址脱敏成 `scheme://host/***`。

    钉钉/飞书机器人的地址通常把 token 放在 path 或 query 中，
    原样展示等于把推送凭据泄露给所有能打开后台的人。
    """
    if not url:
        return ""
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return "已配置（地址格式无法解析）"
    return f"{parsed.scheme}://{parsed.netloc}/***"


async def collect_status(
    session: AsyncSession,
    *,
    mqtt,
    started_at: datetime,
    uptime_seconds: int,
    version: str,
) -> SystemStatusOut:
    """汇总当前运行状态。

    mqtt 直接复用网关进程内的客户端实例，避免为探活额外建连；
    其余指标全部来自数据库聚合查询，不读取原始时序明细。
    """
    device_total = int(
        (await session.execute(select(func.count()).select_from(Device))).scalar_one()
    )
    device_online = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Device)
                .where(Device.status == DEVICE_STATUS_ONLINE)
            )
        ).scalar_one()
    )

    record_total = int(
        (await session.execute(select(func.count()).select_from(SensorRecord))).scalar_one()
    )
    oldest, newest = (
        await session.execute(select(func.min(SensorRecord.ts), func.max(SensorRecord.ts)))
    ).one()

    alert_total = int(
        (await session.execute(select(func.count()).select_from(DeviceAlert))).scalar_one()
    )
    alert_open = int(
        (
            await session.execute(
                select(func.count()).select_from(DeviceAlert).where(DeviceAlert.status == "open")
            )
        ).scalar_one()
    )

    backend = "sqlite" if settings.is_sqlite else "postgresql"

    return SystemStatusOut(
        gateway=GatewayStatusOut(
            version=version,
            started_at=started_at,
            uptime_seconds=uptime_seconds,
        ),
        mqtt=MqttStatusOut(
            connected=bool(getattr(mqtt, "is_connected", False)),
            host=settings.mqtt_host,
            port=settings.mqtt_port,
            messages_handled=int(getattr(mqtt, "message_count", 0)),
            last_error=getattr(mqtt, "last_error", None),
        ),
        storage=StorageStatusOut(
            backend=backend,
            device_total=device_total,
            device_online=device_online,
            device_offline=device_total - device_online,
            record_total=record_total,
            record_oldest_at=_as_aware(oldest),
            record_newest_at=_as_aware(newest),
            alert_total=alert_total,
            alert_open=alert_open,
        ),
        scheduler=SchedulerStatusOut(
            offline_timeout_seconds=settings.device_offline_timeout_seconds,
            offline_sweep_interval_seconds=settings.offline_sweep_interval_seconds,
            data_retention_days=settings.data_retention_days,
            data_purge_interval_seconds=settings.data_purge_interval_seconds,
            alert_suppress_seconds=settings.alert_suppress_seconds,
        ),
        notify=NotifyStatusOut(
            webhook_enabled=bool(settings.alert_webhook_url),
            webhook_target=mask_webhook_target(settings.alert_webhook_url),
        ),
        generated_at=datetime.now(timezone.utc),
    )


def _as_aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite 返回 naive datetime，统一补成 UTC，避免前端按本地时区误读。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
