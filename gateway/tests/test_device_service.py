"""设备服务单测：注册、批量注册去重、分组统计与删除保护。"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401  注册 ORM 元数据
from app.database import Base
from app.models import SensorRecord
from app.services import device_service


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


def test_create_and_update_device(tmp_path):
    async def scenario(session):
        device, err = await device_service.create_device(
            session, "dev-a", "车间传感器", "VG-S100", "1.0.0", "一号车间"
        )
        assert device is not None and err == ""

        duplicate, err = await device_service.create_device(
            session, "dev-a", "重复设备", "VG-S100", "1.0.0", "一号车间"
        )
        assert duplicate is None and err == "设备 ID 已存在"

        updated, err = await device_service.update_device(
            session, "dev-a", {"name": "车间传感器 A", "group_name": "二号车间"}
        )
        assert updated.name == "车间传感器 A"
        assert updated.group_name == "二号车间"

    _run(tmp_path, scenario)


def test_bulk_create_skips_duplicates(tmp_path):
    async def scenario(session):
        await device_service.create_device(
            session, "dev-x", "旧设备", "VG-S100", "1.0.0", "默认分组"
        )
        created, skipped = await device_service.bulk_create_devices(
            session,
            [
                {"device_id": "dev-x", "name": "旧设备", "model": "VG-S100",
                 "firmware_version": "1.0.0", "group_name": "默认分组"},
                {"device_id": "dev-y", "name": "新设备 Y", "model": "VG-S100",
                 "firmware_version": "1.0.0", "group_name": "三号车间"},
                {"device_id": "dev-y", "name": "重复行", "model": "VG-S100",
                 "firmware_version": "1.0.0", "group_name": "三号车间"},
            ],
        )
        assert created == ["dev-y"]
        assert skipped == ["dev-x", "dev-y"]

    _run(tmp_path, scenario)


def test_list_groups_counts_online(tmp_path):
    async def scenario(session):
        await device_service.create_device(
            session, "dev-1", "设备一", "VG-S100", "1.0.0", "一号车间"
        )
        await device_service.create_device(
            session, "dev-2", "设备二", "VG-S100", "1.0.0", "一号车间"
        )
        device = await device_service.get_device(session, "dev-1")
        device.status = "online"

        groups = await device_service.list_groups(session)
        assert len(groups) == 1
        assert groups[0]["group_name"] == "一号车间"
        assert groups[0]["total"] == 2
        assert groups[0]["online"] == 1
        assert groups[0]["offline"] == 1

    _run(tmp_path, scenario)


def test_delete_blocked_when_records_exist(tmp_path):
    async def scenario(session):
        await device_service.create_device(
            session, "dev-r", "有数据设备", "VG-S100", "1.0.0", "默认分组"
        )
        session.add(
            SensorRecord(
                device_id="dev-r",
                temperature=25.0,
                humidity=50.0,
                voltage=3.7,
                rssi=-60,
                battery=80,
                seq=1,
                ts=datetime.now(timezone.utc),
            )
        )
        await session.flush()

        ok, err = await device_service.delete_device(session, "dev-r")
        assert ok is False
        assert "历史数据" in err

    _run(tmp_path, scenario)
