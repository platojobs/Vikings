"""v1.27.0：GET /api/stats/overview 的概览聚合字段与真实数据口径。"""

from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.database import Base, get_session
from app.models import Device, DeviceAlert, SensorRecord
from app.security import ROLE_ADMIN, ROLE_VIEWER, create_access_token


class _AppClient:
    def __init__(self, tmp_path):
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'overview.db'}")
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


def _device(device_id: str, status: str, credential: bool, issued_at=None) -> Device:
    return Device(
        device_id=device_id,
        name=f"设备 {device_id}",
        status=status,
        mqtt_credential_issued_at=issued_at if credential else None,
        mqtt_credential_version=1 if credential else None,
    )


def _record(device_id: str, seq: int, ts: datetime) -> SensorRecord:
    return SensorRecord(
        device_id=device_id,
        temperature=25.0,
        humidity=50.0,
        voltage=12.0,
        rssi=-60,
        battery=80,
        seq=seq,
        ts=ts,
    )


def _alert(alert_id: str, device_id: str, level: str, status: str, created_at: datetime) -> DeviceAlert:
    return DeviceAlert(
        alert_id=alert_id,
        device_id=device_id,
        level=level,
        metric="temperature",
        value=40.0,
        threshold=35.0,
        message="温度超限",
        status=status,
        created_at=created_at,
    )


async def _seed(factory, now: datetime):
    async with factory() as session:
        session.add_all(
            [
                _device("dev-0001", "online", True, now),
                _device("dev-0002", "offline", True, now),
                _device("dev-0003", "online", False),
            ]
        )
        session.add_all(
            [
                # 窗口内：当前小时 2 条、2 小时前 1 条；30 小时前的旧数据用于证明 24h 覆盖完整
                _record("dev-0001", 1, now),
                _record("dev-0001", 2, now - timedelta(minutes=5)),
                _record("dev-0002", 3, now - timedelta(hours=2)),
                _record("dev-0002", 4, now - timedelta(hours=30)),
            ]
        )
        session.add_all(
            [
                _alert("alr-1", "dev-0002", "critical", "open", now - timedelta(hours=1)),
                _alert("alr-2", "dev-0001", "warning", "open", now - timedelta(hours=3)),
                _alert("alr-3", "dev-0001", "critical", "acked", now - timedelta(hours=4)),
            ]
        )
        await session.commit()


def test_overview_aggregates_real_data(tmp_path):
    async def scenario(client, factory):
        now = datetime.now(timezone.utc)
        await _seed(factory, now)

        response = await client.get("/api/stats/overview", headers=_headers())
        assert response.status_code == 200, response.text
        data = response.json()["data"]

        assert data["device_total"] == 3
        assert data["device_online"] == 2
        assert data["device_offline"] == 1
        assert data["device_credential_missing"] == 1
        assert data["alert_open"] == 2
        assert data["alert_warning_open"] == 1
        assert data["alert_critical_open"] == 1
        assert len(data["trend"]) == 24
        assert data["trend_complete"] is True
        assert data["first_record_at"]

        # 趋势只统计 24 小时窗口内的真实上报：3 条（30 小时前那条被排除）
        assert sum(point["reports"] for point in data["trend"]) == 3
        # 告警趋势按创建时间统计「新增告警」，含已被确认的历史告警
        assert sum(point["alerts"] for point in data["trend"]) == 3

        abnormal = {item["device_id"]: item for item in data["abnormal_devices"]}
        assert "dev-0002" in abnormal
        assert abnormal["dev-0002"]["reason"] == "alert+offline"
        assert abnormal["dev-0002"]["open_alert_count"] == 1
        assert abnormal["dev-0001"]["reason"] == "alert"
        assert "dev-0003" not in abnormal  # 在线且无未处理告警
        # 有未处理告警的设备优先于仅离线设备
        assert data["abnormal_devices"][0]["open_alert_count"] >= data["abnormal_devices"][-1]["open_alert_count"]

    async def run():
        async with _AppClient(tmp_path) as (client, factory):
            await scenario(client, factory)

    import asyncio

    asyncio.run(run())


def test_overview_marks_trend_incomplete_when_less_than_24h(tmp_path):
    async def run():
        async with _AppClient(tmp_path) as (client, factory):
            now = datetime.now(timezone.utc)
            async with factory() as session:
                session.add(_device("dev-0001", "online", True, now))
                session.add(_record("dev-0001", 1, now - timedelta(minutes=10)))
                await session.commit()

            response = await client.get("/api/stats/overview", headers=_headers())
            data = response.json()["data"]
            assert data["trend_complete"] is False
            assert sum(point["reports"] for point in data["trend"]) == 1
            assert data["trend"][-1]["reports"] == 1
            # 空桶必须是真实的 0，而不是补假点
            assert all(point["alerts"] == 0 for point in data["trend"])

    import asyncio

    asyncio.run(run())


def test_overview_is_read_only_for_viewer(tmp_path):
    async def run():
        async with _AppClient(tmp_path) as (client, factory):
            now = datetime.now(timezone.utc)
            async with factory() as session:
                session.add(_device("dev-0001", "online", True, now))
                await session.commit()

            forbidden = await client.get("/api/stats/overview")
            assert forbidden.status_code == 401

            viewer = await client.get(
                "/api/stats/overview", headers=_headers(ROLE_VIEWER)
            )
            assert viewer.status_code == 200, viewer.text
            assert viewer.json()["data"]["device_total"] == 1

    import asyncio

    asyncio.run(run())
