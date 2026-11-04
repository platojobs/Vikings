"""数据生命周期：records / 已确认告警 / 审计日志的保留清理。

覆盖三个关键口径：
- records 按上报时间 `ts` 删除（90 天）；
- 只删 acked 告警（180 天），open 告警无论多旧都保留；
- 审计日志独立保留期（365 天），比告警更长。
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import background_tasks
from app import models  # noqa: F401  注册元数据
from app.database import Base
from app.models import AuditLog, DeviceAlert, SensorRecord

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
# CI 的 PostgreSQL 作业设置该变量；本地默认用临时 SQLite
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")


def _run(tmp_path, scenario):
    async def wrapper():
        if TEST_POSTGRES_URL:
            engine = create_async_engine(TEST_POSTGRES_URL)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(
                    text("TRUNCATE sensor_record, device_alert, audit_log RESTART IDENTITY")
                )
        else:
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'lifecycle.db'}")
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            result = await scenario(session, factory)
        await engine.dispose()
        return result

    return asyncio.run(wrapper())


def _seed(session):
    old = NOW - timedelta(days=400)
    session.add_all(
        [
            # records：91 天前应删，10 天前应保留
            SensorRecord(
                device_id="dev-1", temperature=25.0, humidity=60.0, voltage=3.7,
                rssi=-70, battery=80, seq=1, ts=NOW - timedelta(days=91),
            ),
            SensorRecord(
                device_id="dev-1", temperature=26.0, humidity=61.0, voltage=3.7,
                rssi=-70, battery=81, seq=2, ts=NOW - timedelta(days=10),
            ),
            # 告警：超期 acked 应删；近期 acked 保留；超期 open 必须保留
            DeviceAlert(
                alert_id="alt-old-acked", device_id="dev-1", level="warning", metric="temperature",
                value=80.0, threshold=75.0, message="超温", ai_generated=False,
                status="acked", created_at=NOW - timedelta(days=200),
            ),
            DeviceAlert(
                alert_id="alt-new-acked", device_id="dev-1", level="warning", metric="temperature",
                value=80.0, threshold=75.0, message="超温", ai_generated=False,
                status="acked", created_at=NOW - timedelta(days=20),
            ),
            DeviceAlert(
                alert_id="alt-old-open", device_id="dev-1", level="critical", metric="voltage",
                value=3.0, threshold=3.2, message="欠压", ai_generated=False,
                status="open", created_at=old,
            ),
            # 审计：超期应删，近期保留
            AuditLog(
                operator="admin", action="login", target_type="user", target_id="admin",
                detail={"source": "web"}, created_at=old,
            ),
            AuditLog(
                operator="admin", action="alert.ack", target_type="alert", target_id="alt-new-acked",
                detail={}, created_at=NOW - timedelta(days=10),
            ),
        ]
    )


def test_purge_only_expired_rows(tmp_path):
    async def scenario(session, factory):
        _seed(session)
        await session.commit()

        counts = await background_tasks.count_expired_data(
            session_factory=factory, now=NOW
        )
        assert counts == {"records": 1, "alerts": 1, "audit_logs": 1}

        # dry-run 不应删除
        before = int(
            (await session.execute(select(func.count()).select_from(SensorRecord))).scalar_one()
        )
        assert before == 2

        deleted = await background_tasks.purge_expired_data(
            session_factory=factory, now=NOW
        )
        assert deleted == {"records": 1, "alerts": 1, "audit_logs": 1}
        return deleted

    _run(tmp_path, scenario)


def test_remaining_rows_after_purge(tmp_path):
    async def scenario(session, factory):
        _seed(session)
        await session.commit()
        await background_tasks.purge_expired_data(session_factory=factory, now=NOW)

        records = (
            await session.execute(select(SensorRecord.seq, SensorRecord.ts))
        ).all()
        assert len(records) == 1 and records[0].seq == 2

        alerts = (
            await session.execute(select(DeviceAlert.alert_id, DeviceAlert.status))
        ).all()
        assert {row.alert_id for row in alerts} == {"alt-new-acked", "alt-old-open"}
        assert dict(alerts)["alt-old-open"] == "open"

        logs = (await session.execute(select(AuditLog.action))).scalars().all()
        assert logs == ["alert.ack"]

    _run(tmp_path, scenario)


def test_retention_defaults_are_documented_policy():
    from app.config import Settings

    settings = Settings(_env_file=None)
    assert settings.data_retention_days == 90
    assert settings.alert_retention_days == 180
    assert settings.audit_retention_days == 365


def test_query_indexes_exist(tmp_path):
    """v1.19 新增索引在 SQLite / PostgreSQL 两种方言下都存在。"""

    async def scenario(_session, _factory):
        return None

    _run(tmp_path, scenario)

    async def check():
        if TEST_POSTGRES_URL:
            engine = create_async_engine(TEST_POSTGRES_URL)
        else:
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'lifecycle.db'}")

        def inspect_indexes(sync_conn):
            inspector = inspect(sync_conn)
            return {
                table: {item["name"] for item in inspector.get_indexes(table)}
                for table in ("device_alert", "audit_log")
            }

        async with engine.connect() as conn:
            names = await conn.run_sync(inspect_indexes)
        await engine.dispose()
        assert {
            "idx_alert_status_created",
            "idx_alert_level_created",
            "idx_alert_status_device",
        } <= names["device_alert"]
        assert "idx_audit_operator_created" in names["audit_log"]

    asyncio.run(check())
