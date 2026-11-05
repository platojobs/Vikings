"""v1.32.0：GET /api/stats/battery-health 的真实数据口径。

覆盖：风险分层（电量 + 预计天数）、衰减斜率、未知设备、分组汇总、趋势、
分组筛选、limit 截断、参数校验与只读权限。
"""

import asyncio
from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.database import Base, get_session
from app.models import Device, DeviceAlert, SensorRecord
from app.security import ROLE_ADMIN, ROLE_VIEWER, create_access_token


class _AppClient:
    def __init__(self, tmp_path):
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'battery.db'}")
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def __aenter__(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async def override_session():
            async with self.factory() as session:
                try:
                    yield session
                except Exception:
                    await session.rollback()
                    raise

        main.app.dependency_overrides[get_session] = override_session
        self.client = AsyncClient(
            transport=ASGITransport(app=main.app), base_url="http://test"
        )
        await self.client.__aenter__()
        return self.client, self.factory

    async def __aexit__(self, *args):
        await self.client.__aexit__(*args)
        main.app.dependency_overrides.clear()
        await self.engine.dispose()


def _headers(role: str = ROLE_ADMIN) -> dict:
    token = create_access_token("tester", role)
    return {"Authorization": f"Bearer {token}"}


def _device(device_id: str, group: str, status: str = "online") -> Device:
    return Device(device_id=device_id, name=f"设备 {device_id}", group_name=group, status=status)


def _record(device_id: str, seq: int, battery: int, ts: datetime, voltage: float = 3.7) -> SensorRecord:
    return SensorRecord(
        device_id=device_id,
        temperature=25.0,
        humidity=50.0,
        voltage=voltage,
        rssi=-60,
        battery=battery,
        seq=seq,
        ts=ts,
    )


def _seed(now: datetime) -> list:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # 每天的记录放在当天 01:00，确保落在窗口内且按天分桶稳定
    day = lambda offset: today - timedelta(days=offset) + timedelta(hours=1)

    devices = [
        _device("dev-critical", "华东"),
        _device("dev-warning", "华东"),
        _device("dev-fastdrain", "华东"),
        _device("dev-healthy", "华南"),
        _device("dev-unknown", "华南", status="offline"),
        _device("dev-stale", "华南", status="offline"),
    ]

    records = [
        _record("dev-critical", 1, 10, day(0)),
        _record("dev-warning", 1, 25, day(0)),
        # 6 天掉 30 个百分点：约 5 点/天，到 15% 只剩 1 天
        *[
            _record("dev-fastdrain", index + 1, 50 - index * 5, day(6 - index))
            for index in range(7)
        ],
        _record("dev-healthy", 1, 95, day(2)),
        _record("dev-healthy", 2, 95, day(1)),
        _record("dev-healthy", 3, 95, day(0)),
        # 窗口外（8 天前）的记录不应把设备算成 known
        _record("dev-stale", 1, 5, day(8)),
    ]

    alert = DeviceAlert(
        alert_id="alr-battery-1",
        device_id="dev-critical",
        level="warning",
        metric="voltage",
        value=3.1,
        threshold=3.2,
        message="电压偏低",
        status="open",
        created_at=now,
    )
    return devices + records + [alert]


def test_battery_health_real_data(tmp_path):
    async def scenario(client, factory):
        now = datetime.now(timezone.utc)
        async with factory() as session:
            session.add_all(_seed(now))
            await session.commit()

        response = await client.get("/api/stats/battery-health", headers=_headers())
        assert response.status_code == 200, response.text
        data = response.json()["data"]

        assert data["device_total"] == 6
        assert data["battery_known"] == 4
        assert data["critical"] == 2
        assert data["warning"] == 1
        assert data["healthy"] == 1
        assert data["unknown"] == 2
        # (10 + 25 + 20 + 95) / 4
        assert data["avg_battery"] == 37.5
        assert data["window_days"] == 7
        assert data["warning_percent"] == 30
        assert data["critical_percent"] == 15

        items = {item["device_id"]: item for item in data["watchlist"]}
        assert data["watchlist_total"] == 3
        assert items["dev-critical"]["risk"] == "critical"
        assert items["dev-critical"]["days_to_critical"] == 0.0
        assert items["dev-critical"]["open_alert_count"] == 1
        assert items["dev-fastdrain"]["risk"] == "critical"
        assert items["dev-fastdrain"]["drain_per_day"] == 5.0
        assert items["dev-fastdrain"]["days_to_critical"] == 1.0
        assert items["dev-warning"]["risk"] == "warning"
        # 电量稳定/回升的设备不进风险清单
        assert "dev-healthy" not in items
        assert "dev-unknown" not in items
        assert "dev-stale" not in items

        # critical 优先；同为 critical 时预计天数更短的排前面
        assert [item["device_id"] for item in data["watchlist"]] == [
            "dev-critical",
            "dev-fastdrain",
            "dev-warning",
        ]

        groups = {group["group_name"]: group for group in data["groups"]}
        assert groups["华东"]["total"] == 3
        assert groups["华东"]["critical"] == 2
        assert groups["华东"]["warning"] == 1
        assert groups["华东"]["unknown"] == 0
        assert groups["华东"]["avg_battery"] == 18.3
        assert groups["华南"]["total"] == 3
        assert groups["华南"]["healthy"] == 1
        assert groups["华南"]["unknown"] == 2
        assert groups["华南"]["avg_battery"] == 95.0

        # 趋势按天汇总有数据的设备：首日只有快耗电设备，今天四台 known 都在
        assert len(data["trend"]) == 7
        assert data["trend"][0]["avg_battery"] == 50.0
        assert data["trend"][0]["samples"] == 1
        assert data["trend"][-1]["avg_battery"] == 37.5
        assert data["trend"][-1]["samples"] == 4

    async def run():
        async with _AppClient(tmp_path) as (client, factory):
            await scenario(client, factory)

    asyncio.run(run())


def test_battery_health_group_filter_and_limit(tmp_path):
    async def scenario(client, factory):
        now = datetime.now(timezone.utc)
        async with factory() as session:
            session.add_all(_seed(now))
            await session.commit()

        filtered = await client.get(
            "/api/stats/battery-health", params={"group": "华东"}, headers=_headers()
        )
        data = filtered.json()["data"]
        assert data["device_total"] == 3
        assert data["critical"] == 2
        assert data["warning"] == 1
        assert data["unknown"] == 0
        assert [group["group_name"] for group in data["groups"]] == ["华东"]
        # 分组过滤后趋势只统计该分组的设备
        assert data["trend"][-1]["avg_battery"] == 18.3

        limited = await client.get(
            "/api/stats/battery-health", params={"limit": 1}, headers=_headers()
        )
        data = limited.json()["data"]
        assert len(data["watchlist"]) == 1
        assert data["watchlist"][0]["device_id"] == "dev-critical"
        # 截断只影响清单，风险总数仍完整
        assert data["watchlist_total"] == 3

    async def run():
        async with _AppClient(tmp_path) as (client, factory):
            await scenario(client, factory)

    asyncio.run(run())


def test_battery_health_validation_and_permissions(tmp_path):
    async def run():
        async with _AppClient(tmp_path) as (client, factory):
            async with factory() as session:
                session.add(_device("dev-0001", "默认分组"))
                await session.commit()

            unauth = await client.get("/api/stats/battery-health")
            assert unauth.status_code == 401
            assert unauth.json()["code"] == 401

            viewer = await client.get(
                "/api/stats/battery-health", headers=_headers(ROLE_VIEWER)
            )
            assert viewer.status_code == 200, viewer.text
            assert viewer.json()["data"]["device_total"] == 1
            assert viewer.json()["data"]["unknown"] == 1

            for params in ({"days": 0}, {"days": 31}, {"limit": 0}, {"limit": 101}):
                bad = await client.get(
                    "/api/stats/battery-health", params=params, headers=_headers()
                )
                assert bad.status_code == 422, (params, bad.text)

    asyncio.run(run())
