"""v1.17.0：设备凭证签发/轮换、客户端只读凭证与 Broker 口令文件格式。"""

import asyncio
import stat

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.config import Settings
from app.database import Base, get_session
from app.models import Device
from app.mqtt.credentials import (
    hash_password,
    read_password_file,
    upsert_credential,
    verify_hash,
)
from app.security import ROLE_ADMIN, create_access_token
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


def _auth_headers() -> dict:
    token = create_access_token("admin", ROLE_ADMIN)
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
