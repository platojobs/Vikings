"""系统状态服务单测：容量聚合、时区归一与 Webhook 脱敏。"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401  注册 ORM 元数据
from app.database import Base
from app.models import Device, DeviceAlert, SensorRecord
from app.services import system_service


def _run(tmp_path, scenario):
    async def wrapper():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'test.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            await scenario(session)
            await session.commit()
        await engine.dispose()

    asyncio.run(wrapper())


def test_collect_status_aggregates_counts(tmp_path, monkeypatch):
    fake_settings = SimpleNamespace(
        is_sqlite=True,
        mqtt_host="broker.local",
        mqtt_port=1883,
        device_offline_timeout_seconds=300,
        offline_sweep_interval_seconds=60,
        data_retention_days=90,
        data_purge_interval_seconds=3600,
        alert_retention_days=180,
        audit_retention_days=365,
        alert_suppress_seconds=300,
        alert_webhook_url="",
    )
    monkeypatch.setattr(system_service, "settings", fake_settings)

    class FakeMqtt:
        is_connected = True
        last_error = None
        message_count = 128

    now = datetime.now(timezone.utc)

    async def scenario(session):
        session.add_all(
            [
                Device(device_id="dev-1", name="设备一", status="online"),
                Device(device_id="dev-2", name="设备二", status="offline"),
                SensorRecord(
                    device_id="dev-1", temperature=25.5, humidity=60.0, voltage=3.7,
                    rssi=-70, battery=88, seq=1, ts=now - timedelta(minutes=10),
                ),
                SensorRecord(
                    device_id="dev-1", temperature=26.0, humidity=61.0, voltage=3.6,
                    rssi=-72, battery=87, seq=2, ts=now,
                ),
                DeviceAlert(
                    alert_id="alt-1", device_id="dev-1", level="warning", metric="voltage",
                    value=3.3, threshold=3.4, message="电压偏低", status="acked",
                ),
                DeviceAlert(
                    alert_id="alt-2", device_id="dev-1", level="critical", metric="temperature",
                    value=80, threshold=75, message="温度过高", status="open",
                ),
            ]
        )

        status = await system_service.collect_status(
            session,
            mqtt=FakeMqtt(),
            started_at=now - timedelta(hours=1),
            uptime_seconds=3600,
            version="1.0.0",
        )

        assert status.gateway.version == "1.0.0"
        assert status.gateway.uptime_seconds == 3600
        assert status.mqtt.connected is True
        assert status.mqtt.messages_handled == 128
        assert status.mqtt.host == "broker.local"
        assert status.storage.backend == "sqlite"
        assert status.storage.device_total == 2
        assert status.storage.device_online == 1
        assert status.storage.device_offline == 1
        assert status.storage.record_total == 2
        assert status.storage.alert_total == 2
        assert status.storage.alert_open == 1
        # SQLite 的 naive datetime 必须补上 UTC 时区
        assert status.storage.record_newest_at is not None
        assert status.storage.record_newest_at.tzinfo is not None
        assert status.notify.webhook_enabled is False
        assert status.notify.webhook_target == ""

    _run(tmp_path, scenario)


def test_mask_webhook_target_hides_token(monkeypatch):
    # 脱敏函数不依赖数据库，单独覆盖三种输入形态
    assert system_service.mask_webhook_target("") == ""
    assert (
        system_service.mask_webhook_target(
            "https://oapi.dingtalk.com/robot/send?access_token=secret-token"
        )
        == "https://oapi.dingtalk.com/***"
    )
    assert system_service.mask_webhook_target("not-a-url") == "已配置（地址格式无法解析）"
