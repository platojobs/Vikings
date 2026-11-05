"""告警通知（v1.31.0）：多规则路由、持久化重试、投递历史、旁路与脱敏。

Webhook 投递使用进程内真实 HTTP 接收器（http.server），不 mock httpx：
断言客户端真的把 JSON 发到了网络上，并校验 HMAC 与去重键。
SMTP 的真实投递由 `scripts/verify_notifications_v131.py` 用本地 SMTP 桩验证。
"""

import asyncio
import hashlib
import hmac
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401  注册 ORM 元数据
from app.database import get_session
from app.main import app
from app.models import (
    AuditLog,
    Device,
    DeviceAlert,
    NotificationConfig,
    NotificationDelivery,
)
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
        smtp_host="127.0.0.1",
        smtp_from="alerts@vikings.local",
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


async def _create_rule(client, **overrides) -> dict:
    body = {
        "name": "值班规则",
        "enabled": True,
        "priority": 100,
        "levels": ["critical"],
        "groups": [],
        "webhook_enabled": True,
        "webhook_url": "http://127.0.0.1:9/unused",
        "webhook_secret": "rule-secret-should-not-leak",
        "smtp_enabled": False,
        "smtp_to": None,
    }
    body.update(overrides)
    resp = await client.post(
        "/api/system/notifications/rules", json=body, headers=_headers(ROLE_ADMIN, "admin")
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _deliveries(factory) -> list[NotificationDelivery]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(NotificationDelivery).order_by(NotificationDelivery.id)
                )
            )
            .scalars()
            .all()
        )


def test_rule_condition_combination():
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


def test_webhook_real_delivery_hmac_mute_and_dedupe_key(tmp_path):
    receiver = _Receiver()
    try:
        async def scenario(client, factory):
            await _seed_device(factory, "dev-a")
            await _seed_alerts(factory, "dev-a", "voltage", 3)
            # 无规则时回退到 v1.29 单行配置，保持旧配置可用
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
            assert payload["deliveryId"] == 1
            assert request["headers"]["X-Vikings-Delivery-Id"] == "1"
            expected = "sha256=" + hmac.new(
                b"hmac-secret-for-test", request["body"], hashlib.sha256
            ).hexdigest()
            assert request["headers"]["X-Vikings-Signature"] == expected

            rows = await _deliveries(factory)
            statuses = sorted(row.status for row in rows)
            assert statuses == ["sent", "suppressed", "suppressed"]
            suppressed = [row for row in rows if row.status == "suppressed"]
            assert all(row.channel == "mute" for row in suppressed)
            assert all(row.finished_at is not None for row in rows)
            # 静默只影响通知，不影响告警入库与 REST 可见性
            async with factory() as session:
                alerts = (await session.execute(select(DeviceAlert))).scalars().all()
                assert len(alerts) == 3

            resp = await client.get("/api/alerts", headers=_headers(ROLE_OPERATOR, "ops"))
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


def test_retry_queue_survives_restart_and_marks_failed_at_limit(tmp_path):
    receiver = _Receiver(status=500)
    db_file = tmp_path / "notify.db"

    async def scenario(client, factory):
        await _seed_device(factory, "dev-fail")
        await _seed_alerts(factory, "dev-fail", "rssi", 1)
        await _seed_config(
            factory,
            webhook_enabled=True,
            webhook_url=receiver.url,
            webhook_max_attempts=3,
            retry_backoff_seconds=0,
            mute_minutes=0,
        )
        result = await notify_service.dispatch_alert(_alert("dev-fail", "rssi"), session_factory=factory)
        assert result[0]["status"] == "pending" and result[0]["attempts"] == 1
        assert len(receiver.requests) == 1

        # 模拟进程被杀：丢弃原引擎，另起一个全新引擎/会话（同一 SQLite 文件）
        await factory.kw["bind"].dispose()
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
        restarted = async_sessionmaker(engine, expire_on_commit=False)

        # 接收端恢复 200 → 重启后第一轮续投应成功
        receiver.status = 200
        assert await notify_service.retry_due_deliveries(restarted) == 1
        rows = await _deliveries(restarted)
        assert rows[0].status == "sent" and rows[0].attempts == 2
        assert rows[0].finished_at is not None and rows[0].next_attempt_at is None
        assert len(receiver.requests) == 2

        # 再发一条 + 持续 500：续投到上限后终态 failed，原因可见且不再重试
        receiver.status = 500
        await notify_service.dispatch_alert(_alert("dev-fail", "voltage"), session_factory=restarted)
        await notify_service.retry_due_deliveries(restarted)
        await notify_service.retry_due_deliveries(restarted)
        rows = await _deliveries(restarted)
        failed = [row for row in rows if row.metric == "voltage"]
        assert len(failed) == 1
        assert failed[0].status == "failed"
        assert failed[0].attempts == 3
        assert failed[0].error == "HTTP 500"
        assert failed[0].next_attempt_at is None and failed[0].finished_at is not None
        assert await notify_service.retry_due_deliveries(restarted) == 0
        await engine.dispose()

    _run(tmp_path, scenario)
    receiver.close()


def test_rules_route_by_group_priority_and_react_to_group_change(tmp_path):
    east = _Receiver()
    south = _Receiver()
    catch_all = _Receiver()
    try:
        async def scenario(client, factory):
            await _seed_config(factory, enabled=True, mute_minutes=0, retry_backoff_seconds=0)
            for device_id, group in (
                ("dev-east", "华东"),
                ("dev-south", "华南"),
                ("dev-north", "西北"),
            ):
                await _seed_device(factory, device_id, group)

            await _create_rule(client, name="华东值班", priority=10, groups=["华东"], webhook_url=east.url)
            await _create_rule(client, name="华南值班", priority=20, groups=["华南"], webhook_url=south.url)

            await notify_service.dispatch_alert(_alert("dev-east"), session_factory=factory)
            await notify_service.dispatch_alert(_alert("dev-south"), session_factory=factory)
            north = await notify_service.dispatch_alert(_alert("dev-north"), session_factory=factory)

            assert len(east.requests) == 1 and len(south.requests) == 1
            assert north[0]["status"] == "suppressed" and north[0]["channel"] == "rule"
            assert "未匹配任何通知规则" in north[0]["error"]
            rows = await _deliveries(factory)
            assert any(row.channel == "rule" and row.status == "suppressed" for row in rows)

            # 分组变更后下一条告警立即按新分组路由（分组名实时读设备元数据）
            async with factory() as session:
                device = await session.get(Device, "dev-east")
                device.group_name = "华南"
                await session.commit()
            before_east = len(east.requests)
            await notify_service.dispatch_alert(_alert("dev-east", metric="humidity"), session_factory=factory)
            assert len(east.requests) == before_east, "旧分组规则不应再收到"
            assert len(south.requests) == 2, "新分组规则应立即生效"

            # 更高优先级的兜底规则（priority=1, groups 空）优先于两条分组规则
            await _create_rule(client, name="兜底值班", priority=1, groups=[], webhook_url=catch_all.url)
            await notify_service.dispatch_alert(_alert("dev-south", metric="battery"), session_factory=factory)
            assert len(catch_all.requests) == 1
            assert len(south.requests) == 2

            # 列表按 priority ASC, id ASC 返回，界面据此展示裁决顺序
            listed = (
                await client.get("/api/system/notifications/rules", headers=_headers(ROLE_ADMIN, "admin"))
            ).json()["data"]
            assert [item["name"] for item in listed] == ["兜底值班", "华东值班", "华南值班"]

        _run(tmp_path, scenario)
    finally:
        east.close()
        south.close()
        catch_all.close()


def test_rule_api_masks_secrets_and_audits_field_names_only(tmp_path):
    receiver = _Receiver()
    try:
        async def scenario(client, factory):
            rule = await _create_rule(
                client,
                name="脱敏规则",
                webhook_url=receiver.url,
                webhook_secret="secret-should-not-leak",
            )
            text = json.dumps(rule, ensure_ascii=False)
            assert "secret-should-not-leak" not in text
            assert receiver.url not in text
            assert rule["webhook_url_configured"] is True
            assert rule["webhook_secret_configured"] is True

            resp = await client.patch(
                f"/api/system/notifications/rules/{rule['id']}",
                json={"priority": 5, "webhook_url": receiver.url},
                headers=_headers(ROLE_ADMIN, "admin"),
            )
            assert resp.status_code == 200, resp.text
            assert "secret-should-not-leak" not in resp.text
            assert receiver.url not in resp.text

            async with factory() as session:
                audits = (await session.execute(select(AuditLog))).scalars().all()
            actions = [row.action for row in audits]
            assert "notify.rule_create" in actions and "notify.rule_update" in actions
            serialized = json.dumps([row.detail for row in audits], ensure_ascii=False)
            assert "secret-should-not-leak" not in serialized
            assert receiver.url not in serialized
            update = next(row for row in audits if row.action == "notify.rule_update")
            assert update.detail["rule_id"] == rule["id"]
            assert "priority" in update.detail["fields"]
            assert "webhook_url" in update.detail["fields"]

            # 权限：viewer/operator 不能读写规则，operator 可看投递历史，viewer 不行
            viewer_get = await client.get(
                "/api/system/notifications/rules", headers=_headers(ROLE_VIEWER, "viewer")
            )
            assert viewer_get.status_code == 403
            operator_create = await client.post(
                "/api/system/notifications/rules",
                json={"name": "越权", "webhook_enabled": True, "webhook_url": receiver.url},
                headers=_headers(ROLE_OPERATOR, "ops"),
            )
            assert operator_create.status_code == 403
            assert (await client.get(
                "/api/system/notifications/deliveries", headers=_headers(ROLE_OPERATOR, "ops")
            )).status_code == 200
            assert (await client.get(
                "/api/system/notifications/deliveries", headers=_headers(ROLE_VIEWER, "viewer")
            )).status_code == 403

        _run(tmp_path, scenario)
    finally:
        receiver.close()


def test_delivery_history_filters_and_pagination(tmp_path):
    async def scenario(client, factory):
        await _seed_config(factory, enabled=False)
        now = datetime.now(timezone.utc)
        async with factory() as session:
            entries = [
                ("webhook", "sent", "dev-a", now - timedelta(hours=5)),
                ("webhook", "failed", "dev-a", now - timedelta(hours=3)),
                ("smtp", "sent", "dev-b", now - timedelta(hours=1)),
                ("rule", "suppressed", "dev-c", now - timedelta(minutes=10)),
                ("smtp", "failed", "dev-b", now),
            ]
            for index, (channel, status, device_id, created) in enumerate(entries):
                session.add(
                    NotificationDelivery(
                        alert_id=f"alt-hist-{index}",
                        device_id=device_id,
                        metric="voltage",
                        level="critical",
                        channel=channel,
                        status=status,
                        attempts=3 if status == "failed" else 1,
                        error="HTTP 500" if status == "failed" else None,
                        duration_ms=120,
                        finished_at=created,
                        created_at=created,
                        updated_at=created,
                    )
                )
            await session.commit()

        resp = await client.get(
            "/api/system/notifications/deliveries",
            params={"page": 1, "page_size": 2},
            headers=_headers(ROLE_OPERATOR, "ops"),
        )
        data = resp.json()["data"]
        assert data["total"] == 5 and len(data["items"]) == 2
        assert data["items"][0]["device_id"] == "dev-b", "按时间倒序"

        filtered = (
            await client.get(
                "/api/system/notifications/deliveries",
                params={"status": "failed", "channel": "smtp"},
                headers=_headers(ROLE_OPERATOR, "ops"),
            )
        ).json()["data"]
        assert filtered["total"] == 1
        assert filtered["items"][0]["error"] == "HTTP 500"
        assert filtered["items"][0]["attempts"] == 3
        assert filtered["items"][0]["duration_ms"] == 120
        assert filtered["items"][0]["finished_at"] is not None

        by_device = (
            await client.get(
                "/api/system/notifications/deliveries",
                params={"device_id": "dev-a"},
                headers=_headers(ROLE_ADMIN, "admin"),
            )
        ).json()["data"]
        assert by_device["total"] == 2

        ranged = (
            await client.get(
                "/api/system/notifications/deliveries",
                params={
                    "start": (now - timedelta(hours=2)).isoformat(),
                    "end": now.isoformat(),
                },
                headers=_headers(ROLE_OPERATOR, "ops"),
            )
        ).json()["data"]
        assert ranged["total"] == 3, ranged

        page2 = (
            await client.get(
                "/api/system/notifications/deliveries",
                params={"page": 2, "page_size": 2},
                headers=_headers(ROLE_OPERATOR, "ops"),
            )
        ).json()["data"]
        assert page2["page"] == 2 and len(page2["items"]) == 2

    _run(tmp_path, scenario)


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


def test_test_notification_endpoint_uses_rule_target(tmp_path):
    receiver = _Receiver()
    try:
        async def scenario(client, factory):
            await _seed_config(factory, enabled=True, smtp_host="127.0.0.1", smtp_from="a@b.c")
            rule = await _create_rule(client, webhook_url=receiver.url, webhook_secret="test-secret")
            resp = await client.post(
                "/api/system/notifications/test",
                json={"channel": "webhook", "rule_id": rule["id"]},
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
                assert rows[0].rule_name == "值班规则"

        _run(tmp_path, scenario)
    finally:
        receiver.close()


def test_dispatch_is_safe_when_config_disabled(tmp_path):
    async def scenario(client, factory):
        await _seed_config(factory, enabled=False)
        results = await notify_service.dispatch_alert(_alert("dev-a"), session_factory=factory)
        assert results == []

    _run(tmp_path, scenario)
