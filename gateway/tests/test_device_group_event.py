"""设备分组变更实时事件单测：报文契约、边界与发布降级。"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401  注册 ORM 元数据
from app.database import Base
from app.services import device_service
from app.topics import TOPIC_SYSTEM_GROUP


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


class _FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    async def __call__(self, topic, payload):
        if self.fail:
            raise RuntimeError("MQTT Broker 未连接")
        self.calls.append((topic, payload))


def test_group_change_payload_contract():
    """事件字段与 docs/contracts.md 2.6 一致，ts 为毫秒时间戳。"""
    device = SimpleNamespace(device_id="dev-0001", group_name="二号车间")
    ts = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)

    payload = device_service.group_change_payload(
        device, "一号车间", "admin", ts
    )

    assert payload == {
        "deviceId": "dev-0001",
        "groupName": "二号车间",
        "previousGroupName": "一号车间",
        "operator": "admin",
        "ts": int(ts.timestamp() * 1000),
    }


def test_update_device_group_change_returns_event(tmp_path):
    async def scenario(session):
        await device_service.create_device(
            session, "dev-a", "车间传感器", "VG-S100", "1.0.0", "一号车间"
        )

        device, err, event = await device_service.update_device_with_group_event(
            session, "dev-a", {"group_name": "二号车间"}, "admin"
        )

        assert err == ""
        assert device.group_name == "二号车间"
        assert event is not None
        assert event["deviceId"] == "dev-a"
        assert event["groupName"] == "二号车间"
        assert event["previousGroupName"] == "一号车间"
        assert event["operator"] == "admin"
        assert isinstance(event["ts"], int)

    _run(tmp_path, scenario)


def test_update_device_group_unchanged_publishes_nothing(tmp_path):
    """只改名称或重复提交同一分组都不产生事件，只改名称等既有行为不变。"""
    async def scenario(session):
        await device_service.create_device(
            session, "dev-a", "车间传感器", "VG-S100", "1.0.0", "一号车间"
        )

        device, _, renamed = await device_service.update_device_with_group_event(
            session, "dev-a", {"name": "车间传感器 A"}, "admin"
        )
        assert device.name == "车间传感器 A"
        assert renamed is None

        _, _, same_group = await device_service.update_device_with_group_event(
            session, "dev-a", {"group_name": "一号车间"}, "admin"
        )
        assert same_group is None

    _run(tmp_path, scenario)


def test_update_device_missing_device_returns_no_event(tmp_path):
    async def scenario(session):
        device, err, event = await device_service.update_device_with_group_event(
            session, "dev-missing", {"group_name": "二号车间"}, "admin"
        )
        assert device is None
        assert err == "设备不存在"
        assert event is None

    _run(tmp_path, scenario)


def test_publish_group_change_uses_system_topic(tmp_path):
    async def scenario(_session):
        payload = {"deviceId": "dev-a", "groupName": "二号车间"}
        publisher = _FakePublisher()

        assert await device_service.publish_group_change(publisher, payload) is True
        assert publisher.calls == [(TOPIC_SYSTEM_GROUP, payload)]

    _run(tmp_path, scenario)


def test_publish_group_change_degrades_without_raising(tmp_path):
    """发布失败只返回 False，不改变 REST 编辑的成功语义。"""
    async def scenario(_session):
        publisher = _FakePublisher(fail=True)
        payload = {"deviceId": "dev-a", "groupName": "二号车间"}

        assert await device_service.publish_group_change(publisher, payload) is False
        assert publisher.calls == []

    _run(tmp_path, scenario)
