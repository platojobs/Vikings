"""HTTP 层回归：告警批量确认权限/部分失败，以及 PATCH 设备在 MQTT 不可用时仍 200。

这些用例复用真实 FastAPI 路由与依赖（只替换数据库会话和 MQTT 发布器），
用于补齐此前只有 service 层与 E2E 覆盖的 PATCH HTTP 行为。
"""

import asyncio
from datetime import datetime, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401  注册 ORM 元数据
from app.database import Base, get_session
from app.main import app
from app.models import Device, DeviceAlert
from app.security import ROLE_OPERATOR, ROLE_VIEWER, create_access_token


class _BrokenMqtt:
    """模拟 Broker 未连接：发布必须抛异常，由服务层降级吞掉。"""

    calls = []

    async def publish(self, topic, payload):
        self.calls.append((topic, payload))
        raise RuntimeError("MQTT Broker 未连接")


async def _run_with_app(tmp_path, scenario):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'http.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = override_session
    mqtt = _BrokenMqtt()
    app.state.mqtt = mqtt
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await scenario(client, factory, mqtt)
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


def _run(tmp_path, scenario):
    asyncio.run(_run_with_app(tmp_path, scenario))


def _headers(role: str, username: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role=role)}"}


async def _seed_device(factory, device_id: str = "dev-a", group_name: str = "一号车间"):
    async with factory() as session:
        session.add(
            Device(
                device_id=device_id,
                name="车间传感器",
                model="VG-S100",
                firmware_version="1.0.0",
                group_name=group_name,
                status="online",
            )
        )
        await session.commit()


async def _seed_alert(factory, alert_id: str, status: str = "open"):
    async with factory() as session:
        session.add(
            DeviceAlert(
                alert_id=alert_id,
                device_id="dev-a",
                level="critical",
                metric="voltage",
                value=2.9,
                threshold=3.2,
                message="电压过低",
                ai_generated=False,
                status=status,
                created_at=datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc),
            )
        )
        await session.commit()


def test_patch_device_with_mqtt_down_returns_200(tmp_path):
    """MQTT 发布失败只降级，不改变 PATCH 的成功语义，更不能抛 5xx。"""

    async def scenario(client, factory, mqtt):
        await _seed_device(factory)
        resp = await client.patch(
            "/api/devices/dev-a",
            json={"group_name": "二号车间"},
            headers=_headers(ROLE_OPERATOR, "operator1"),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["group_name"] == "二号车间"
        # 证明确实尝试发布并失败，而不是因参数未变化跳过
        assert len(mqtt.calls) == 1
        assert mqtt.calls[0][0] == "device/system/group"

        detail = await client.get(
            "/api/devices/dev-a", headers=_headers(ROLE_VIEWER, "viewer1")
        )
        assert detail.status_code == 200
        assert detail.json()["data"]["group_name"] == "二号车间"

    _run(tmp_path, scenario)


def test_alert_list_http_filters_and_pagination(tmp_path):
    """HTTP 层验证 level/status/deviceId 组合筛选与分页 total。"""

    async def scenario(client, factory, _mqtt):
        for index in range(5):
            await _seed_alert(factory, f"alt-{index}", status="open" if index < 3 else "acked")
        headers = _headers(ROLE_VIEWER, "viewer1")

        resp = await client.get(
            "/api/alerts",
            params={"deviceId": "dev-a", "level": "critical", "status": "open", "page": 1, "pageSize": 2},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["total"] == 3
        assert len(data["items"]) == 2
        assert data["page"] == 1 and data["page_size"] == 2

        # 非法 level 仍应被参数校验拒绝（避免脏查询拖垮数据库）
        bad = await client.get("/api/alerts", params={"level": "unknown"}, headers=headers)
        assert bad.status_code == 422

    _run(tmp_path, scenario)


def test_bulk_ack_http_partial_failure_and_permissions(tmp_path):
    """operator 批量确认可见部分失败；viewer 403；未登录 401。"""

    async def scenario(client, factory, _mqtt):
        await _seed_device(factory)
        await _seed_alert(factory, "alt-open", status="open")
        await _seed_alert(factory, "alt-acked", status="acked")

        payload = {"alert_ids": ["alt-open", "alt-acked", "alt-missing"]}
        resp = await client.post(
            "/api/alerts/bulk-ack",
            json=payload,
            headers=_headers(ROLE_OPERATOR, "operator1"),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["acked"] == ["alt-open"]
        assert data["already_acked"] == ["alt-acked"]
        assert data["not_found"] == ["alt-missing"]
        assert data["requested"] == 3

        open_resp = await client.get(
            "/api/alerts", params={"status": "open"}, headers=_headers(ROLE_VIEWER, "viewer1")
        )
        assert open_resp.json()["data"]["total"] == 0

        forbidden = await client.post(
            "/api/alerts/bulk-ack",
            json={"alert_ids": ["alt-open"]},
            headers=_headers(ROLE_VIEWER, "viewer1"),
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == 403

        unauthorized = await client.post("/api/alerts/bulk-ack", json={"alert_ids": ["alt-open"]})
        assert unauthorized.status_code == 401

        # 空列表由 Pydantic 拦截，不进入业务层
        empty = await client.post(
            "/api/alerts/bulk-ack",
            json={"alert_ids": []},
            headers=_headers(ROLE_OPERATOR, "operator1"),
        )
        assert empty.status_code == 422

    _run(tmp_path, scenario)
