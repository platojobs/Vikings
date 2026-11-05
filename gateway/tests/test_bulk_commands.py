"""批量指令端点（v1.28.0）：部分失败、权限、幂等、审计与 OTA 隔离。

复用真实 FastAPI 路由与依赖，只替换数据库会话和 MQTT 发布器。
"""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import models  # noqa: F401  注册 ORM 元数据
from app.database import get_session
from app.main import app
from app.models import AuditLog, Device, DeviceCommand
from app.security import ROLE_OPERATOR, ROLE_VIEWER, create_access_token
from db_utils import create_test_engine


class _PublishMqtt:
    """记录发布会话；`fail_topic_contains` 用于模拟单台设备的投递失败。"""

    def __init__(self, fail_topic_contains: str = "") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_topic_contains = fail_topic_contains

    async def publish(self, topic, payload):
        self.calls.append((topic, payload))
        if self.fail_topic_contains and self.fail_topic_contains in topic:
            raise RuntimeError("MQTT 发布失败")


def _run(tmp_path, scenario, *, fail_topic_contains: str = ""):
    async def wrapper():
        engine = await create_test_engine(tmp_path, "bulk.db")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        mqtt = _PublishMqtt(fail_topic_contains)

        async def override_session():
            async with factory() as session:
                try:
                    yield session
                except Exception:
                    await session.rollback()
                    raise

        app.dependency_overrides[get_session] = override_session
        app.state.mqtt = mqtt
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await scenario(client, factory, mqtt)
        finally:
            app.dependency_overrides.clear()
            await engine.dispose()

    asyncio.run(wrapper())


def _headers(role: str, username: str = "tester") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role=role)}"}


async def _seed(factory, device_id: str, *, status: str = "online", credential: bool = True):
    async with factory() as session:
        session.add(
            Device(
                device_id=device_id,
                name="批量设备 " + device_id,
                firmware_version="1.0.0",
                status=status,
                mqtt_credential_issued_at=datetime.now(timezone.utc) if credential else None,
                mqtt_credential_version=1 if credential else 0,
            )
        )
        await session.commit()


def test_bulk_command_reports_success_failed_skipped(tmp_path):
    async def scenario(client, factory, mqtt):
        await _seed(factory, "dev-ok")
        await _seed(factory, "dev-nocred", status="offline", credential=False)
        await _seed(factory, "dev-offline", status="offline")

        resp = await client.post(
            "/api/devices/commands/bulk",
            json={
                "device_ids": ["dev-ok", "dev-nocred", "dev-offline", "dev-missing", "dev-ok"],
                "type": "config",
                "payload": {"reportIntervalSec": 30},
            },
            headers=_headers(ROLE_OPERATOR),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["requested"] == 4  # 重复设备 ID 去重
        assert [item["device_id"] for item in data["success"]] == ["dev-ok"]
        skipped = {item["device_id"]: item["reason"] for item in data["skipped"]}
        assert set(skipped) == {"dev-nocred", "dev-offline", "dev-missing"}
        assert "凭证" in skipped["dev-nocred"]
        assert "离线" in skipped["dev-offline"]
        assert "不存在" in skipped["dev-missing"]
        assert data["failed"] == []
        assert len(mqtt.calls) == 1
        assert mqtt.calls[0][0] == f"device/dev-ok/cmd"

        async with factory() as session:
            rows = (await session.execute(select(DeviceCommand))).scalars().all()
            assert len(rows) == 1 and rows[0].state == "issued"
            audits = (await session.execute(select(AuditLog))).scalars().all()
            assert len(audits) == 1
            assert audits[0].action == "command.bulk_create"
            detail = audits[0].detail
            assert detail["requested"] == 4
            assert detail["successCount"] == 1
            assert detail["skippedCount"] == 3

    _run(tmp_path, scenario)


def test_bulk_command_single_publish_failure_does_not_abort_batch(tmp_path):
    async def scenario(client, factory, mqtt):
        await _seed(factory, "dev-ok")
        await _seed(factory, "dev-fail")

        resp = await client.post(
            "/api/devices/commands/bulk",
            json={"device_ids": ["dev-ok", "dev-fail"], "type": "reboot", "payload": {}},
            headers=_headers(ROLE_OPERATOR),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert [item["device_id"] for item in data["success"]] == ["dev-ok"]
        assert data["failed"][0]["device_id"] == "dev-fail"
        assert "MQTT" in data["failed"][0]["reason"]

        async with factory() as session:
            states = {
                row.device_id: row.state
                for row in (await session.execute(select(DeviceCommand))).scalars().all()
            }
            assert states == {"dev-ok": "issued", "dev-fail": "failed"}

    _run(tmp_path, scenario, fail_topic_contains="/dev-fail/")


def test_bulk_command_is_idempotent_for_same_request_id(tmp_path):
    request_id = "req-" + uuid4().hex[:12]

    async def scenario(client, factory, mqtt):
        await _seed(factory, "dev-ok")
        body = {
            "device_ids": ["dev-ok"],
            "type": "config",
            "payload": {"reportIntervalSec": 30},
            "request_id": request_id,
        }
        first = await client.post(
            "/api/devices/commands/bulk", json=body, headers=_headers(ROLE_OPERATOR)
        )
        second = await client.post(
            "/api/devices/commands/bulk", json=body, headers=_headers(ROLE_OPERATOR)
        )
        assert first.status_code == second.status_code == 200
        a, b = first.json()["data"], second.json()["data"]
        assert b["replayed"] is True
        assert a["batch_id"] == b["batch_id"]
        assert a["success"] == b["success"]
        assert len(mqtt.calls) == 1

        async with factory() as session:
            commands = (await session.execute(select(DeviceCommand))).scalars().all()
            audits = (await session.execute(select(AuditLog))).scalars().all()
            assert len(commands) == 1
            assert len(audits) == 1

    _run(tmp_path, scenario)


def test_bulk_command_rejects_viewer_and_ota_type(tmp_path):
    async def scenario(client, factory, mqtt):
        await _seed(factory, "dev-ok")

        viewer = await client.post(
            "/api/devices/commands/bulk",
            json={"device_ids": ["dev-ok"], "type": "config", "payload": {}},
            headers=_headers(ROLE_VIEWER),
        )
        assert viewer.status_code == 403

        ota = await client.post(
            "/api/devices/commands/bulk",
            json={"device_ids": ["dev-ok"], "type": "ota", "payload": {}},
            headers=_headers(ROLE_OPERATOR),
        )
        assert ota.status_code == 422
        assert mqtt.calls == []

    _run(tmp_path, scenario)
