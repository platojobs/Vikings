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


def _add_record(session, device_id: str, ts: datetime, seq: int = 1) -> None:
    session.add(
        SensorRecord(
            device_id=device_id,
            temperature=25.0,
            humidity=50.0,
            voltage=3.7,
            rssi=-60,
            battery=80,
            seq=seq,
            ts=ts,
        )
    )


def test_list_devices_default_query_unchanged(tmp_path):
    """回归：不传 status/keyword 时返回全部设备，默认分页 20 条。"""
    async def scenario(session):
        for index in range(3):
            await device_service.create_device(
                session, f"dev-{index}", f"设备 {index}", "VG-S100", "1.0.0", "默认分组"
            )

        items, total = await device_service.list_devices(session, None, None, 1, 20)
        assert total == 3
        assert [item.device_id for item in items] == ["dev-0", "dev-1", "dev-2"]

    _run(tmp_path, scenario)


def test_list_devices_filters_and_pagination(tmp_path):
    async def scenario(session):
        await device_service.create_device(
            session, "dev-on-1", "一号机", "VG-S100", "1.0.0", "甲组"
        )
        await device_service.create_device(
            session, "dev-on-2", "二号机", "VG-S100", "1.0.0", "甲组"
        )
        await device_service.create_device(
            session, "dev-off", "备用机", "VG-S100", "1.0.0", "乙组"
        )
        for device_id in ("dev-on-1", "dev-on-2"):
            (await device_service.get_device(session, device_id)).status = "online"

        online, total = await device_service.list_devices(session, "online", None, 1, 20)
        assert total == 2
        assert {item.device_id for item in online} == {"dev-on-1", "dev-on-2"}

        # 关键词命中名称或设备 ID（大小写不敏感）
        hit, total = await device_service.list_devices(session, None, "二号", 1, 20)
        assert total == 1
        assert hit[0].device_id == "dev-on-2"

        # 分页只影响返回条目，total 仍是过滤后的总数
        page1, total = await device_service.list_devices(session, None, None, 1, 1)
        page2, _ = await device_service.list_devices(session, None, None, 2, 1)
        assert total == 3
        assert len(page1) == 1 and len(page2) == 1
        assert page1[0].device_id != page2[0].device_id

    _run(tmp_path, scenario)


def test_latest_records_default_query_unchanged(tmp_path):
    """回归：不传任何参数时返回全部设备各自最新一条，按时间倒序。"""
    async def scenario(session):
        await device_service.create_device(
            session, "dev-a", "A 设备", "VG-S100", "1.0.0", "默认分组"
        )
        await device_service.create_device(
            session, "dev-b", "B 设备", "VG-S100", "1.0.0", "默认分组"
        )
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _add_record(session, "dev-a", base)
        _add_record(session, "dev-a", base.replace(hour=2), seq=2)
        _add_record(session, "dev-b", base.replace(hour=1), seq=3)
        await session.flush()

        rows = await device_service.latest_records_all(session)
        assert [row.device_id for row in rows] == ["dev-a", "dev-b"]
        assert [row.seq for row in rows] == [2, 3]

    _run(tmp_path, scenario)


def test_latest_records_filters_and_pagination(tmp_path):
    async def scenario(session):
        await device_service.create_device(
            session, "dev-a", "温度采集 A", "VG-S100", "1.0.0", "默认分组"
        )
        await device_service.create_device(
            session, "dev-b", "温度采集 B", "VG-S100", "1.0.0", "默认分组"
        )
        (await device_service.get_device(session, "dev-b")).status = "online"
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _add_record(session, "dev-a", base)
        _add_record(session, "dev-b", base.replace(hour=1), seq=2)
        await session.flush()

        # 状态过滤：只返回在线设备
        online = await device_service.latest_records_all(session, status="online")
        assert [row.device_id for row in online] == ["dev-b"]

        # 关键词过滤：命中设备名称
        hit = await device_service.latest_records_all(session, keyword="采集 a")
        assert [row.device_id for row in hit] == ["dev-a"]

        # limit / offset：在默认全量结果上切片
        assert [row.device_id for row in await device_service.latest_records_all(session, limit=1)] == [
            "dev-b"
        ]
        assert [
            row.device_id for row in await device_service.latest_records_all(session, limit=1, offset=1)
        ] == ["dev-a"]

    _run(tmp_path, scenario)
