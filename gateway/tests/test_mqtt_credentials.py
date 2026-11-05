"""v1.17.0：设备凭证签发/轮换、客户端只读凭证与 Broker 口令文件格式。"""

import asyncio
import base64
import stat

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.config import Settings
from app.database import Base, get_session
from app.models import AuditLog, Device
from app.mqtt.credentials import (
    hash_password,
    read_password_file,
    upsert_credential,
    verify_hash,
)
from app.security import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, create_access_token
from app.services import mqtt_credential_service


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "app_env": "test",
        "mqtt_username": "iot-gateway",
        "mqtt_password": "GatewayBrokerSecret!2026",
        "mqtt_password_file": str(tmp_path / "mosquitto" / "passwd"),
        "mqtt_reload_command": "true",
        "mqtt_web_username": "web-console",
        "mqtt_web_password": "WebBrokerSecret!2026",
        "mqtt_mobile_username": "mobile-console",
        "mqtt_mobile_password": "MobileBrokerSecret!2026",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_hash_format_is_mosquitto_pbkdf2_sha512():
    stored = hash_password("device-secret")
    assert stored.startswith("$7$1000$")
    assert verify_hash("device-secret", stored)
    assert not verify_hash("wrong-secret", stored)


def test_hash_salt_is_12_bytes_for_mosquitto_2_0_compat():
    """Ubuntu 自带的 Mosquitto 2.0.x 只接受 12 字节盐，2.1.x 接受 12/64。"""
    stored = hash_password("device-secret")
    _empty, prefix, iterations, salt_b64, digest_b64 = stored.split("$")
    assert prefix == "7"
    assert int(iterations) == 1000
    assert len(base64.b64decode(salt_b64)) == 12
    assert len(base64.b64decode(digest_b64)) == 64


def test_password_file_contains_hash_only(tmp_path):
    path = tmp_path / "passwd"
    upsert_credential(path, "dev-0001", "PlainDeviceSecret")
    text = path.read_text(encoding="utf-8")
    assert "PlainDeviceSecret" not in text
    stored = read_password_file(path)["dev-0001"]
    assert stored.startswith("$7$")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


class _AppClient:
    def __init__(self, tmp_path):
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'mqtt.db'}")
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


def _auth_headers(role: str = ROLE_ADMIN, username: str = "admin") -> dict:
    token = create_access_token(username, role)
    return {"Authorization": f"Bearer {token}"}


def test_device_credential_issue_and_rotate(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(mqtt_credential_service, "get_settings", lambda: settings)

    async def scenario(client, factory):
        async with factory() as session:
            session.add(Device(device_id="dev-0001", name="一号设备", status="offline"))
            await session.commit()

        headers = _auth_headers()
        first = await client.post("/api/devices/dev-0001/mqtt-credential", headers=headers)
        assert first.status_code == 200, first.text
        payload = first.json()["data"]
        assert payload["username"] == "dev-0001"
        assert payload["version"] == 1
        assert payload["password"]
        assert payload["reloaded"] is True
        first_password = payload["password"]

        password_file = settings.mqtt_password_path
        assert first_password not in password_file.read_text(encoding="utf-8")
        assert verify_hash(
            first_password, read_password_file(password_file)["dev-0001"]
        )

        second = await client.post("/api/devices/dev-0001/mqtt-credential", headers=headers)
        assert second.status_code == 200, second.text
        second_payload = second.json()["data"]
        assert second_payload["version"] == 2
        assert second_payload["password"] != first_password
        stored = read_password_file(password_file)["dev-0001"]
        assert not verify_hash(first_password, stored)
        assert verify_hash(second_payload["password"], stored)

        async with factory() as session:
            device = await session.scalar(select(Device).where(Device.device_id == "dev-0001"))
            assert device is not None
            assert device.mqtt_username == "dev-0001"
            assert device.mqtt_credential_version == 2
            assert device.mqtt_credential_fingerprint
            assert device.mqtt_credential_issued_at is not None
            assert device.mqtt_credential_rotated_at is not None
            assert first_password not in (device.mqtt_credential_fingerprint or "")
            assert second_payload["password"] not in (device.mqtt_credential_fingerprint or "")

    asyncio.run(_scenario(tmp_path, scenario))


def _scenario(tmp_path, scenario):
    async def wrapper():
        async with _AppClient(tmp_path) as (client, factory):
            await scenario(client, factory)

    return wrapper()


def test_client_credentials_are_read_only_and_independent(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(mqtt_credential_service, "get_settings", lambda: settings)

    async def scenario(client, _factory):
        web = await client.get(
            "/api/auth/mqtt-credentials", params={"client": "web"}, headers=_auth_headers()
        )
        assert web.status_code == 200, web.text
        web_data = web.json()["data"]
        assert web_data["username"] == "web-console"
        assert web_data["read_only"] is True
        assert "device/+/sensor/report" in web_data["topics"]

        mobile = await client.get(
            "/api/auth/mqtt-credentials", params={"client": "mobile"}, headers=_auth_headers()
        )
        assert mobile.status_code == 200, mobile.text
        mobile_data = mobile.json()["data"]
        assert mobile_data["username"] == "mobile-console"
        assert mobile_data["password"] != web_data["password"]
        assert mobile_data["read_only"] is True

        invalid = await client.get(
            "/api/auth/mqtt-credentials", params={"client": "other"}, headers=_auth_headers()
        )
        assert invalid.status_code == 422

    asyncio.run(_scenario(tmp_path, scenario))


def test_client_credentials_503_when_password_missing(tmp_path, monkeypatch):
    settings = _settings(tmp_path, mqtt_web_password="")
    monkeypatch.setattr(mqtt_credential_service, "get_settings", lambda: settings)

    async def scenario(client, _factory):
        response = await client.get(
            "/api/auth/mqtt-credentials", params={"client": "web"}, headers=_auth_headers()
        )
        assert response.status_code == 503
        body = response.json()
        message = body.get("message") or body.get("detail") or ""
        assert "MQTT_WEB_PASSWORD" in message

    asyncio.run(_scenario(tmp_path, scenario))


def test_credential_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(mqtt_credential_service, "get_settings", lambda: _settings(tmp_path))

    async def scenario(client, _factory):
        response = await client.post("/api/devices/dev-0001/mqtt-credential")
        assert response.status_code == 401

    asyncio.run(_scenario(tmp_path, scenario))


def test_credential_permission_matrix_and_revoke(tmp_path, monkeypatch, caplog):
    """签发/轮换/吊销的 RBAC：viewer 全禁；operator 只能首次签发；轮换/吊销仅 admin。"""
    settings = _settings(tmp_path)
    monkeypatch.setattr(mqtt_credential_service, "get_settings", lambda: settings)
    caplog.set_level("INFO")

    async def scenario(client, factory):
        async with factory() as session:
            session.add(Device(device_id="dev-0001", name="一号设备", status="offline"))
            await session.commit()

        viewer = _auth_headers(ROLE_VIEWER, "viewer1")
        operator = _auth_headers(ROLE_OPERATOR, "operator1")
        admin = _auth_headers(ROLE_ADMIN, "admin1")

        assert (
            await client.post("/api/devices/dev-0001/mqtt-credential", headers=viewer)
        ).status_code == 403
        assert (
            await client.delete("/api/devices/dev-0001/mqtt-credential", headers=viewer)
        ).status_code == 403

        issued = await client.post("/api/devices/dev-0001/mqtt-credential", headers=operator)
        assert issued.status_code == 200, issued.text
        issued_data = issued.json()["data"]
        assert issued_data["operation"] == "issue"
        assert issued_data["version"] == 1
        first_password = issued_data["password"]
        assert first_password in issued.text, "首次签发必须把口令返回给调用方一次"

        # operator 不能覆盖已有有效凭证
        forbidden = await client.post(
            "/api/devices/dev-0001/mqtt-credential", headers=operator
        )
        assert forbidden.status_code == 403, forbidden.text
        assert "仅管理员可执行" in forbidden.text

        rotated = await client.post("/api/devices/dev-0001/mqtt-credential", headers=admin)
        assert rotated.status_code == 200, rotated.text
        rotated_data = rotated.json()["data"]
        assert rotated_data["operation"] == "rotate"
        assert rotated_data["version"] == 2
        assert rotated_data["password"] != first_password

        assert (
            await client.delete("/api/devices/dev-0001/mqtt-credential", headers=operator)
        ).status_code == 403

        revoked = await client.delete(
            "/api/devices/dev-0001/mqtt-credential", headers=admin
        )
        assert revoked.status_code == 200, revoked.text
        revoked_data = revoked.json()["data"]
        assert revoked_data["reloaded"] is True
        assert "password" not in revoked.text

        detail = await client.get("/api/devices/dev-0001", headers=admin)
        assert detail.status_code == 200
        detail_data = detail.json()["data"]
        assert detail_data["mqtt_credential_status"] == "revoked"
        assert detail_data["mqtt_credential_revoked_at"]
        assert "password" not in detail.text

        # 已吊销再吊销：409，不重复写审计
        again = await client.delete("/api/devices/dev-0001/mqtt-credential", headers=admin)
        assert again.status_code == 409

        # 吊销后 operator 可重新签发，并清除吊销状态
        reissued = await client.post(
            "/api/devices/dev-0001/mqtt-credential", headers=operator
        )
        assert reissued.status_code == 200, reissued.text
        assert reissued.json()["data"]["operation"] == "issue"
        detail_after = await client.get("/api/devices/dev-0001", headers=admin)
        assert detail_after.json()["data"]["mqtt_credential_status"] == "active"
        assert detail_after.json()["data"]["mqtt_credential_revoked_at"] is None

        async with factory() as session:
            logs = (
                await session.scalars(select(AuditLog).order_by(AuditLog.id))
            ).all()
            credential_logs = [
                log
                for log in logs
                if log.action
                in {
                    "device.credential_issue",
                    "device.credential_revoke",
                }
            ]
            assert [log.action for log in credential_logs] == [
                "device.credential_issue",
                "device.credential_issue",
                "device.credential_revoke",
                "device.credential_issue",
            ]
            issue_detail = credential_logs[0].detail
            assert issue_detail["operation"] == "issue"
            assert issue_detail["result"] == "reload_ok"
            assert issue_detail["reloaded"] is True
            assert issue_detail["version"] == 1
            rotate_detail = credential_logs[1].detail
            assert rotate_detail["operation"] == "rotate"
            assert rotate_detail["version"] == 2
            revoke_detail = credential_logs[2].detail
            assert revoke_detail["result"] == "reload_ok"
            assert revoke_detail["username"] == "dev-0001"

            device = await session.scalar(select(Device).where(Device.device_id == "dev-0001"))
            assert device is not None
            full_fingerprint = device.mqtt_credential_fingerprint
            assert full_fingerprint

        # 响应与日志都不得出现明文口令或完整指纹
        assert first_password not in detail.text
        assert rotated_data["password"] not in detail.text
        assert full_fingerprint not in detail.text
        assert full_fingerprint not in caplog.text
        detail_prefix = detail_after.json()["data"]["mqtt_credential_fingerprint_prefix"]
        assert detail_prefix
        assert len(detail_prefix) == 8
        assert detail_prefix == full_fingerprint[:8]

        listing = await client.get("/api/devices", headers=admin)
        assert listing.status_code == 200
        assert rotated_data["password"] not in listing.text
        assert full_fingerprint not in listing.text
        assert all(
            "password" not in item
            for item in listing.json()["data"]["items"]
        )

    asyncio.run(_scenario(tmp_path, scenario))


def test_credential_reload_failure_is_reported_and_audited(
    tmp_path, monkeypatch, caplog
):
    """SIGHUP / MQTT_RELOAD_COMMAND 失败时必须如实报告，并写入审计结果字段。"""
    settings = _settings(tmp_path)
    monkeypatch.setattr(mqtt_credential_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        mqtt_credential_service,
        "reload_broker",
        lambda: (False, "reload command failed: exit code 1"),
    )
    caplog.set_level("INFO")

    async def scenario(client, factory):
        async with factory() as session:
            session.add(Device(device_id="dev-0001", name="一号设备", status="offline"))
            await session.commit()

        admin = _auth_headers()
        response = await client.post("/api/devices/dev-0001/mqtt-credential", headers=admin)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["reloaded"] is False
        assert "reload command failed" in data["reload_detail"]
        password = data["password"]
        assert password

        # 口令已写入，但 Broker 未重载：界面/文档用 reloaded=false 判断真实状态
        assert verify_hash(
            password, read_password_file(settings.mqtt_password_path)["dev-0001"]
        )
        assert password not in caplog.text

        async with factory() as session:
            log = await session.scalar(
                select(AuditLog).where(
                    AuditLog.action == "device.credential_issue"
                )
            )
            assert log is not None
            assert log.detail["reloaded"] is False
            assert log.detail["result"] == "reload_failed"
            assert "reload command failed" in log.detail["reload_detail"]

        # 吊销同样必须报告未重载
        revoked = await client.delete("/api/devices/dev-0001/mqtt-credential", headers=admin)
        assert revoked.status_code == 200, revoked.text
        revoked_data = revoked.json()["data"]
        assert revoked_data["reloaded"] is False
        async with factory() as session:
            log = await session.scalar(
                select(AuditLog).where(
                    AuditLog.action == "device.credential_revoke"
                )
            )
            assert log is not None
            assert log.detail["result"] == "reload_failed"

    asyncio.run(_scenario(tmp_path, scenario))
