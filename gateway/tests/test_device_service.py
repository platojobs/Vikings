"""设备服务单测：注册、批量注册去重、分组统计与删除保护。"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401  注册 ORM 元数据
from app.database import Base
from app.models import DeviceAlert, SensorRecord
from app.services import alert_service, device_service


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


def test_list_devices_pagination_boundaries(tmp_path):
    """首屏 / 翻页 / 末页 / 越界 page：total 始终为过滤后总数，越界返回空列表。"""
    async def scenario(session):
        for index in range(25):
            await device_service.create_device(
                session, f"dev-{index:02d}", f"设备 {index}", "VG-S100", "1.0.0", "默认分组"
            )

        page1, total = await device_service.list_devices(session, None, None, 1, 10)
        page2, _ = await device_service.list_devices(session, None, None, 2, 10)
        page3, _ = await device_service.list_devices(session, None, None, 3, 10)
        page99, total99 = await device_service.list_devices(session, None, None, 99, 10)

        assert total == 25 and total99 == 25
        assert [item.device_id for item in page1] == [f"dev-{i:02d}" for i in range(10)]
        assert [item.device_id for item in page2] == [f"dev-{i:02d}" for i in range(10, 20)]
        assert [item.device_id for item in page3] == [f"dev-{i:02d}" for i in range(20, 25)]
        assert page99 == []

    _run(tmp_path, scenario)


def _add_alert(session, alert_id: str, device_id: str, status: str = "open") -> None:
    session.add(
        DeviceAlert(
            alert_id=alert_id,
            device_id=device_id,
            level="warning",
            metric="temperature",
            value=80.0,
            threshold=75.0,
            message="测试告警",
            ai_generated=False,
            status=status,
        )
    )


def test_list_devices_has_alert_filter_and_aggregate_counts(tmp_path):
    """有告警筛选按数据库聚合，与告警列表分页大小无关；计数按设备正确归并。"""
    async def scenario(session):
        for index in range(3):
            await device_service.create_device(
                session, f"dev-{index}", f"设备 {index}", "VG-S100", "1.0.0", "默认分组"
            )
        _add_alert(session, "alt-a1", "dev-0")
        _add_alert(session, "alt-a2", "dev-0")
        _add_alert(session, "alt-b1", "dev-1")
        _add_alert(session, "alt-c1", "dev-2", status="acked")
        await session.flush()

        devices, total = await device_service.list_devices(
            session, None, None, 1, 20, has_alert=True
        )
        assert total == 2
        assert [item.device_id for item in devices] == ["dev-0", "dev-1"]

        # 分页边界下 total 仍为聚合后的总数，不受 pageSize 影响
        _page1, total_small = await device_service.list_devices(
            session, None, None, 1, 1, has_alert=True
        )
        assert total_small == 2

        counts = await alert_service.open_alert_counts_by_device(
            session, ["dev-0", "dev-1", "dev-2"]
        )
        assert counts == {"dev-0": 2, "dev-1": 1}
        assert await alert_service.open_alert_counts_by_device(session, []) == {}

        # 组合筛选：有告警 + 关键词，只命中 dev-0
        both, total_both = await device_service.list_devices(
            session, None, "设备 0", 1, 20, has_alert=True
        )
        assert total_both == 1
        assert both[0].device_id == "dev-0"

    _run(tmp_path, scenario)


def test_list_devices_default_new_params_off(tmp_path):
    """回归：不传 has_alert 时结果与显式 False 完全一致。"""
    async def scenario(session):
        for index in range(3):
            await device_service.create_device(
                session, f"dev-{index}", f"设备 {index}", "VG-S100", "1.0.0", "默认分组"
            )
            _add_alert(session, f"alt-{index}", f"dev-{index}")
        await session.flush()

        default, total_default = await device_service.list_devices(session, None, None, 1, 20)
        explicit, total_explicit = await device_service.list_devices(
            session, None, None, 1, 20, has_alert=False
        )
        assert total_default == total_explicit == 3
        assert [item.device_id for item in default] == [item.device_id for item in explicit]

    _run(tmp_path, scenario)


def test_latest_records_device_ids_filter(tmp_path):
    async def scenario(session):
        for device_id in ("dev-a", "dev-b", "dev-c"):
            await device_service.create_device(
                session, device_id, device_id, "VG-S100", "1.0.0", "默认分组"
            )
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _add_record(session, "dev-a", base)
        _add_record(session, "dev-b", base.replace(hour=1), seq=2)
        _add_record(session, "dev-c", base.replace(hour=2), seq=3)
        await session.flush()

        # 只取当前页设备，顺序仍按时间倒序
        rows = await device_service.latest_records_all(session, device_ids=["dev-a", "dev-c"])
        assert [row.device_id for row in rows] == ["dev-c", "dev-a"]

        # 空列表直接返回空，不触发全量查询
        assert await device_service.latest_records_all(session, device_ids=[]) == []

        # 回归：不传 device_ids 时仍返回全部设备各自最新一条
        assert [row.device_id for row in await device_service.latest_records_all(session)] == [
            "dev-c",
            "dev-b",
            "dev-a",
        ]

    _run(tmp_path, scenario)


def test_group_filter_and_ungrouped_bucket(tmp_path):
    """分组筛选：空分组与字面「未分组」合并，且可与关键词、有告警组合。"""
    async def scenario(session):
        await device_service.create_device(
            session, "dev-prod", "生产设备", "VG-S100", "1.0.0", "生产组"
        )
        await device_service.create_device(
            session, "dev-empty", "空分组设备", "VG-S100", "1.0.0", ""
        )
        await device_service.create_device(
            session, "dev-literal", "未分组设备", "VG-S100", "1.0.0", "未分组"
        )
        await device_service.create_device(
            session, "dev-default", "默认设备", "VG-S100", "1.0.0", "默认分组"
        )
        _add_alert(session, "alt-empty", "dev-empty")
        await session.flush()

        groups = await device_service.list_groups(session)
        assert {item["group_name"]: item["total"] for item in groups} == {
            "生产组": 1,
            "未分组": 2,
            "默认分组": 1,
        }
        assert [item["group_name"] for item in groups] == sorted(
            item["group_name"] for item in groups
        )

        ungrouped, ungrouped_total = await device_service.list_devices(
            session, None, None, 1, 20, group_name="未分组"
        )
        assert ungrouped_total == 2
        assert {item.device_id for item in ungrouped} == {"dev-empty", "dev-literal"}

        prod, prod_total = await device_service.list_devices(
            session, None, None, 1, 20, group_name="生产组"
        )
        assert prod_total == 1
        assert prod[0].device_id == "dev-prod"

        # 分组 + 关键词 + 有告警 组合筛选
        both, both_total = await device_service.list_devices(
            session, None, "空分组", 1, 20, group_name="未分组", has_alert=True
        )
        assert both_total == 1
        assert both[0].device_id == "dev-empty"

        # 回归：不传 group 时总数不变
        _all, all_total = await device_service.list_devices(session, None, None, 1, 20)
        assert all_total == 4

    _run(tmp_path, scenario)


def test_query_records_default_query_unchanged(tmp_path):
    """回归：不传 offset 时仍返回最近 limit 条，且与显式 offset=0 完全一致。"""
    async def scenario(session):
        await device_service.create_device(
            session, "dev-r", "历史设备", "VG-S100", "1.0.0", "默认分组"
        )
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(5):
            _add_record(session, "dev-r", base.replace(hour=index), seq=index + 1)
        await session.flush()

        default = await device_service.query_records(session, "dev-r", None, None, 3)
        explicit = await device_service.query_records(session, "dev-r", None, None, 3, 0)

        # 取最新 3 条（seq 3/4/5），服务层统一翻转为时间正序
        assert [row.seq for row in default] == [3, 4, 5]
        assert [row.seq for row in explicit] == [3, 4, 5]

    _run(tmp_path, scenario)


def test_query_records_offset_pages_backwards(tmp_path):
    """offset 从最新一条开始跳过，逐页向更早取数；越界返回空列表。"""
    async def scenario(session):
        await device_service.create_device(
            session, "dev-r", "历史设备", "VG-S100", "1.0.0", "默认分组"
        )
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(5):
            _add_record(session, "dev-r", base.replace(hour=index), seq=index + 1)
        await session.flush()

        first = await device_service.query_records(session, "dev-r", None, None, 2)
        second = await device_service.query_records(session, "dev-r", None, None, 2, 2)
        last = await device_service.query_records(session, "dev-r", None, None, 2, 4)
        beyond = await device_service.query_records(session, "dev-r", None, None, 2, 99)

        assert [row.seq for row in first] == [4, 5]
        assert [row.seq for row in second] == [2, 3]
        assert [row.seq for row in last] == [1]
        assert beyond == []

    _run(tmp_path, scenario)
