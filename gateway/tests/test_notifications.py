"""告警通知（v1.29.0）：规则组合、静默抑制、重试留痕、旁路不阻塞、脱敏与权限。

Webhook 投递使用进程内真实 HTTP 接收器（http.server），不 mock httpx：
断言客户端真的把 JSON 发到了网络上，并校验 HMAC 签名。
SMTP 的真实投递由 `scripts/verify_notifications.py` 用本地 SMTP 桩验证。
"""

import asyncio
import hashlib
import hmac
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import models  # noqa: F401  注册 ORM 元数据
from app.database import get_session
from app.main import app
from app.models import AuditLog, Device, DeviceAlert, NotificationConfig, NotificationDelivery
from app.security import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, create_access_token
from app.services import notify_service
from db_utils import create_test_engine


class _Receiver:
    """真实本地 HTTP 接收器：记录请求体/头，可配置状态码与延迟。"""

    def __init__(self, *, status: int = 200, delay: float = 0.0) -> None:
        self.requests: list[dict] = []
        self.status = status
        self.delay = delay
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                receiver.requests.append(
                    {"headers": dict(self.headers), "body": body, "path": self.path}
                )
                if receiver.delay:
                    threading.Event().wait(receiver.delay)
                self.send_response(receiver.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *_args):  # 静默访问日志
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/hook"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _run(tmp_path, scenario):
    async def wrapper():
        engine = await create_test_engine(tmp_path, "notify.db")
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def override_session():
            async with factory() as session:
                try:
                    yield session
                except Exception:
                    await session.rollback()
                    raise

        app.dependency_overrides[get_session] = override_session
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await scenario(client, factory)
        finally:
            app.dependency_overrides.clear()
            await engine.dispose()

    asyncio.run(wrapper())


def _headers(role: str, username: str = "tester") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role=role)}"}


async def _seed_config(factory, **overrides) -> None:
    values = dict(
        id=1,
        enabled=True,
        webhook_enabled=False,
        smtp_enabled=False,
        levels="critical",
        groups="",
        device_allow="",
        device_deny="",
        mute_minutes=10,
        retry_backoff_seconds=0,
        webhook_timeout_seconds=5,
        webhook_max_attempts=1,
        smtp_timeout_seconds=5,
        smtp_max_attempts=1,
        updated_by="system",
    )
    values.update(overrides)
    async with factory() as session:
        session.add(NotificationConfig(**values))
        await session.commit()


async def _seed_device(factory, device_id: str, group_name: str = "默认分组") -> None:
    async with factory() as session:
        session.add(
            Device(device_id=device_id, name="通知设备", group_name=group_name, status="online")
        )
        await session.commit()


async def _seed_alerts(factory, device_id: str, metric: str, count: int) -> None:
    async with factory() as session:
        for index in range(count):
            session.add(
                DeviceAlert(
                    alert_id=f"alt-{device_id}-{metric}-{index}",
                    device_id=device_id,
                    level="critical",
                    metric=metric,
                    value=2.9,
                    threshold=3.2,
                    message="电压过低",
                    ai_generated=False,
                    status="open",
                    created_at=datetime.now(timezone.utc),
                )
            )
        await session.commit()


def _alert(device_id: str, metric: str = "voltage", level: str = "critical") -> dict:
    return {
        "alertId": f"alt-{device_id}-{metric}",
        "deviceId": device_id,
        "deviceName": "通知设备",
        "level": level,
        "metric": metric,
        "value": 2.9,
        "threshold": 3.2,
        "message": "电压过低",
    }


def test_rule_combination_level_group_allow_deny():
    class _Cfg:
        levels = "critical"
        groups = "华东"
        device_allow = "dev-a,dev-b"
        device_deny = "dev-b"

    cfg = _Cfg()
    assert notify_service.matches_rules(cfg, _alert("dev-a"), "华东")
    # 级别不匹配
    assert not notify_service.matches_rules(cfg, _alert("dev-a", level="warning"), "华东")
    # 分组不匹配
    assert not notify_service.matches_rules(cfg, _alert("dev-a"), "华南")
    # 白名单外
    assert not notify_service.matches_rules(cfg, _alert("dev-c"), "华东")
    # 黑名单优先于白名单
    assert not notify_service.matches_rules(cfg, _alert("dev-b"), "华东")

    cfg.groups = ""
    cfg.device_allow = ""
    cfg.device_deny = ""
    assert notify_service.matches_rules(cfg, _alert("dev-x"), "")


def test_webhook_real_delivery_hmac_and_mute(tmp_path):
    receiver = _Receiver()
    try:
        async def scenario(client, factory):
            await _seed_device(factory, "dev-a")
            await _seed_alerts(factory, "dev-a", "voltage", 3)
            await _seed_config(
                factory,
                webhook_enabled=True,
                webhook_url=receiver.url,
                webhook_secret="hmac-secret-for-test",
                mute_minutes=60,
            )

            for _ in range(3):
                await notify_service.dispatch_alert(_alert("dev-a"), session_factory=factory)

            assert len(receiver.requests) == 1, "静默窗口内只应真实投递 1 次"
            request = receiver.requests[0]
            payload = json.loads(request["body"])
            assert payload["event"] == "alert"
            assert payload["alert"]["deviceId"] == "dev-a"
            assert payload["device"]["groupName"] == "默认分组"
            expected = "sha256=" + hmac.new(
                b"hmac-secret-for-test", request["body"], hashlib.sha256
            ).hexdigest()
            assert request["headers"]["X-Vikings-Signature"] == expected

            async with factory() as session:
                deliveries = (
                    (await session.execute(select(NotificationDelivery))).scalars().all()
                )
                statuses = sorted(row.status for row in deliveries)
                assert statuses == ["sent", "suppressed", "suppressed"]
                suppressed = [row for row in deliveries if row.status == "suppressed"]
                assert all(row.channel == "mute" for row in suppressed)
                # 静默只影响通知，不影响告警入库与 REST 可见性
                alerts = (await session.execute(select(DeviceAlert))).scalars().all()
                assert len(alerts) == 3

            resp = await client.get(
                "/api/alerts", headers=_headers(ROLE_OPERATOR, "ops")
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["data"]["total"] == 3

        _run(tmp_path, scenario)
    finally:
        receiver.close()


def test_webhook_signature_mismatch_is_detectable(tmp_path):
    receiver = _Receiver()
    try:
        async def scenario(client, factory):
            await _seed_device(factory, "dev-a")
            await _seed_config(
                factory,
                webhook_enabled=True,
                webhook_url=receiver.url,
                webhook_secret="right-secret",
                mute_minutes=0,
            )
            await notify_service.dispatch_alert(_alert("dev-a"), session_factory=factory)
            request = receiver.requests[0]
            wrong = "sha256=" + hmac.new(b"wrong-secret", request["body"], hashlib.sha256).hexdigest()
            assert request["headers"]["X-Vikings-Signature"] != wrong

        _run(tmp_path, scenario)
    finally:
        receiver.close()


def test_failed_webhook_retries_then_records_error_and_alert_survives(tmp_path):
    receiver = _Receiver(status=500)
    try:
        async def scenario(client, factory):
            await _seed_device(factory, "dev-a")
            await _seed_alerts(factory, "dev-a", "voltage", 1)
            await _seed_config(
                factory,
                webhook_enabled=True,
                webhook_url=receiver.url,
                webhook_max_attempts=3,
                retry_backoff_seconds=0,
                mute_minutes=0,
            )
            results = await notify_service.dispatch_alert(
                _alert("dev-a"), session_factory=factory
            )
            assert len(receiver.requests) == 3
            assert results[0]["status"] == "failed"
            assert results[0]["attempts"] == 3
            assert results[0]["error"] == "HTTP 500"

            async with factory() as session:
                row = (
                    await session.execute(select(NotificationDelivery))
                ).scalars().one()
                assert row.status == "failed"
                assert row.attempts == 3
                assert row.error == "HTTP 500"
                assert "127.0.0.1" not in (row.error or "")
                alerts = (await session.execute(select(DeviceAlert))).scalars().all()
                assert len(alerts) == 1, "通知失败不得回滚或删除告警"

        _run(tmp_path, scenario)
    finally:
        receiver.close()


def test_config_masks_secrets_and_audit_only_field_names(tmp_path):
    receiver = _Receiver()
    try:
        async def scenario(client, factory):
            body = {
                "enabled": True,
                "webhook_enabled": True,
                "webhook_url": receiver.url,
                "webhook_secret": "secret-should-not-leak",
                "smtp_enabled": True,
                "smtp_host": "mail.internal",
                "smtp_username": "alerts@internal",
                "smtp_password": "smtp-should-not-leak",
                "smtp_from": "alerts@internal",
                "smtp_to": "oncall@internal",
                "levels": ["critical"],
                "mute_minutes": 10,
            }
            resp = await client.patch(
                "/api/system/notifications/config",
                json=body,
                headers=_headers(ROLE_ADMIN, "admin"),
            )
            assert resp.status_code == 200, resp.text
            text = resp.text
            assert "secret-should-not-leak" not in text
            assert "smtp-should-not-leak" not in text
            assert "127.0.0.1" not in text
            data = resp.json()["data"]
            assert data["webhook_url_configured"] is True
            assert data["webhook_secret_configured"] is True
            assert data["smtp_password_configured"] is True

            fetched = await client.get(
                "/api/system/notifications/config",
                headers=_headers(ROLE_ADMIN, "admin"),
            )
            assert fetched.status_code == 200
            assert "secret-should-not-leak" not in fetched.text
            assert "smtp-should-not-leak" not in fetched.text

            async with factory() as session:
                audit = (await session.execute(select(AuditLog))).scalars().one()
                assert audit.action == "notify.config_update"
                serialized = json.dumps(audit.detail, ensure_ascii=False)
                assert "secret-should-not-leak" not in serialized
                assert "smtp-should-not-leak" not in serialized
                assert "webhook_secret" in audit.detail["fields"]
                assert "smtp_password" in audit.detail["fields"]

            # 权限：operator 与 viewer 都不能改配置
            denied = await client.patch(
                "/api/system/notifications/config",
                json={"enabled": False},
                headers=_headers(ROLE_OPERATOR, "ops"),
            )
            assert denied.status_code == 403
            viewer_get = await client.get(
                "/api/system/notifications/config",
                headers=_headers(ROLE_VIEWER, "viewer"),
            )
            assert viewer_get.status_code == 403

        _run(tmp_path, scenario)
    finally:
        receiver.close()


def test_test_notification_endpoint_really_delivers(tmp_path):
    receiver = _Receiver()
    try:
        async def scenario(client, factory):
            await _seed_config(
                factory,
                webhook_enabled=True,
                webhook_url=receiver.url,
                webhook_secret="test-secret",
            )
            resp = await client.post(
                "/api/system/notifications/test",
                json={"channel": "webhook"},
                headers=_headers(ROLE_OPERATOR, "ops"),
            )
            assert resp.status_code == 200, resp.text
            result = resp.json()["data"][0]
            assert result["channel"] == "webhook"
            assert result["status"] == "sent"
            assert len(receiver.requests) == 1
            headers = receiver.requests[0]["headers"]
            assert headers["X-Vikings-Test"] == "1"
            assert json.loads(receiver.requests[0]["body"])["event"] == "notification.test"

            async with factory() as session:
                rows = (await session.execute(select(NotificationDelivery))).scalars().all()
                assert len(rows) == 1 and rows[0].status == "sent"

        _run(tmp_path, scenario)
    finally:
        receiver.close()


def test_dispatch_is_safe_when_config_disabled(tmp_path):
    async def scenario(client, factory):
        await _seed_config(factory, enabled=False)
        results = await notify_service.dispatch_alert(
            _alert("dev-a"), session_factory=factory
        )
        assert results == []

    _run(tmp_path, scenario)
