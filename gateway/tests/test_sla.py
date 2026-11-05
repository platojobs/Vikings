"""告警 SLA 与值班交接（v1.33.0）。

覆盖：
- 三态口径与历史 acked_at=NULL 行归为 unknown（不计达标）；
- 列表 SLA 字段与「已超时」筛选；
- 超时升级真实 HTTP 投递、同告警同步不重复、达上限停止、重启（新引擎）不重发；
- 值班交接责任落库 + 审计 + 接收人一条汇总通知 + 不重置计时/升级计数；
- SLA 配置权限（admin 可改、viewer 只读）。
"""

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import models  # noqa: F401  注册 ORM 元数据
from app.database import get_session
from app.main import app
from app.models import (
    AuditLog,
    Device,
    DeviceAlert,
    NotificationConfig,
    NotificationDelivery,
    SlaConfig,
)
from app.security import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, create_access_token
from app.services import sla_service
from db_utils import create_test_engine


class _Receiver:
    """真实本地 HTTP 接收器：断言 JSON 真的发到了网络上，而不是 mock httpx。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                receiver.requests.append(json.loads(self.rfile.read(length) or b"{}"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *_args):
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


def _headers(role: str, username: str = "tester") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role=role)}"}


def _run(tmp_path, scenario, filename: str = "sla.db"):
    async def wrapper():
        engine = await create_test_engine(tmp_path, filename)
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


def _alert(
    alert_id: str,
    *,
    status: str = "open",
    level: str = "critical",
    created_minutes_ago: int = 0,
    sla_minutes: int = 30,
    acked_minutes_ago: int | None = None,
    assignee: str | None = None,
    escalation_count: int = 0,
) -> DeviceAlert:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    created = now - timedelta(minutes=created_minutes_ago)
    return DeviceAlert(
        alert_id=alert_id,
        device_id="dev-sla",
        level=level,
        metric="voltage",
        value=2.9,
        threshold=3.2,
        message="电压过低",
        ai_generated=False,
        status=status,
        created_at=created,
        sla_minutes=sla_minutes,
        sla_deadline_at=created + timedelta(minutes=sla_minutes),
        acked_at=(
            None
            if acked_minutes_ago is None
            else now - timedelta(minutes=acked_minutes_ago)
        ),
        acked_by=None if acked_minutes_ago is None else "ops",
        assignee=assignee,
        escalation_count=escalation_count,
    )


def test_sla_state_matrix_marks_legacy_ack_as_unknown():
    """达标 / 超时达成 / 未处理（未超时、已超时）/ 历史确认无时间 五种口径。"""
    config = SlaConfig(id=1, critical_minutes=30, warning_minutes=120)
    # 存储层是 UTC；这里显式用带时区的 now，和线上口径一致
    now = datetime.now(timezone.utc)

    met = _alert("a-met", status="acked", created_minutes_ago=20, acked_minutes_ago=10)
    # 截止时间在 20 分钟前，45 分钟前确认 → 已超时；这里改为 15 分钟前确认才是「超时达成」
    late = _alert("a-late", status="acked", created_minutes_ago=50, acked_minutes_ago=15)
    pending = _alert("a-pending", created_minutes_ago=5)
    overdue = _alert("a-overdue", created_minutes_ago=40)
    unknown = _alert("a-unknown", status="acked", acked_minutes_ago=None)

    assert sla_service.evaluate(met, now, config)["sla_state"] == "met"
    assert sla_service.evaluate(late, now, config)["sla_state"] == "late"
    assert sla_service.evaluate(pending, now, config)["sla_state"] == "pending"
    assert sla_service.evaluate(overdue, now, config)["sla_state"] == "overdue"
    assert sla_service.evaluate(unknown, now, config)["sla_state"] == "unknown"
    # 历史确认行绝不能被当成达标，也不能给出「已响应」秒数
    assert sla_service.evaluate(unknown, now, config)["sla_response_seconds"] is None

    # 剩余/超出时长：未处理为剩余（负数即超出）
    assert sla_service.evaluate(pending, now, config)["sla_remaining_seconds"] > 0
    assert sla_service.evaluate(overdue, now, config)["sla_remaining_seconds"] < 0


def test_list_sla_fields_and_overdue_filter(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            session.add(Device(device_id="dev-sla", name="SLA 设备", status="online"))
            session.add(_alert("a-pending", created_minutes_ago=5))
            session.add(_alert("a-overdue", created_minutes_ago=50))
            session.add(_alert("a-met", status="acked", acked_minutes_ago=10))
            session.add(_alert("a-unknown", status="acked", acked_minutes_ago=None))
            await session.commit()

        resp = await client.get("/api/alerts", headers=_headers(ROLE_OPERATOR, "ops"))
        assert resp.status_code == 200, resp.text
        items = {item["alert_id"]: item for item in resp.json()["data"]["items"]}
        assert items["a-pending"]["sla_state"] == "pending"
        assert items["a-overdue"]["sla_state"] == "overdue"
        assert items["a-met"]["sla_state"] == "met"
        assert items["a-unknown"]["sla_state"] == "unknown"
        assert items["a-overdue"]["sla_remaining_seconds"] < 0

        resp = await client.get(
            "/api/alerts", params={"sla": "overdue"}, headers=_headers(ROLE_OPERATOR, "ops")
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["total"] == 1
        assert [item["alert_id"] for item in data["items"]] == ["a-overdue"]

        summary = await client.get("/api/alerts/summary", headers=_headers(ROLE_VIEWER, "v"))
        assert summary.status_code == 200
        assert summary.json()["data"]["overdue_open"] == 1

    _run(tmp_path, scenario)


def test_sla_config_permissions_and_snapshot(tmp_path):
    async def scenario(client, factory):
        resp = await client.get("/api/alerts/sla-config", headers=_headers(ROLE_VIEWER, "v"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["critical_minutes"] == 30

        resp = await client.put(
            "/api/alerts/sla-config",
            json={"critical_minutes": 5, "warning_minutes": 60, "max_escalations": 1},
            headers=_headers(ROLE_OPERATOR, "ops"),
        )
        assert resp.status_code == 403, resp.text

        resp = await client.put(
            "/api/alerts/sla-config",
            json={"critical_minutes": 5, "warning_minutes": 60, "max_escalations": 1},
            headers=_headers(ROLE_ADMIN, "admin"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["critical_minutes"] == 5

        async with factory() as session:
            audit = (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "sla.config_update")
                )
            ).scalars().all()
            assert len(audit) == 1
            assert audit[0].detail["changed"] == [
                "critical_minutes", "warning_minutes", "max_escalations"
            ]

    _run(tmp_path, scenario)


def _seed_notification(factory, url: str) -> None:
    async def inner():
        async with factory() as session:
            session.add(Device(device_id="dev-sla", name="SLA 设备", status="online"))
            session.add(
                NotificationConfig(
                    id=1,
                    enabled=True,
                    webhook_enabled=True,
                    webhook_url=url,
                    webhook_secret="sla-secret",
                    levels="critical,warning",
                    mute_minutes=0,
                    webhook_max_attempts=2,
                )
            )
            session.add(
                SlaConfig(
                    id=1,
                    critical_minutes=30,
                    warning_minutes=120,
                    max_escalations=2,
                    escalation_interval_minutes=5,
                )
            )
            await session.commit()

    return inner()


def test_escalation_real_http_once_per_step_and_cap(tmp_path):
    receiver = _Receiver()
    try:
        async def scenario(client, factory):
            await _seed_notification(factory, receiver.url)
            async with factory() as session:
                alert = _alert("a-esc", created_minutes_ago=34, escalation_count=0)
                session.add(alert)
                await session.commit()

            first = await sla_service.sweep_escalations(factory)
            assert first == 1
            assert len(receiver.requests) == 1
            payload = receiver.requests[0]
            assert payload["event"] == "alert.escalation"
            assert payload["escalation"]["attempt"] == 1
            assert payload["escalation"]["maxAttempts"] == 2
            assert payload["escalation"]["slaMinutes"] == 30
            assert payload["escalation"]["overdueMinutes"] >= 3

            # 同一步重复巡检（含重启后新会话）：不重复发送、计数不变
            assert await sla_service.sweep_escalations(factory) == 0
            assert len(receiver.requests) == 1

            async with factory() as session:
                stored = await session.get(DeviceAlert, alert.id)
                assert stored.escalation_count == 1

            # 第二轮：deadline + 5 分钟（升级间隔）已到
            later = datetime.now(timezone.utc) + timedelta(minutes=2)
            assert await sla_service.sweep_escalations(factory, now=later) == 1
            assert len(receiver.requests) == 2
            assert receiver.requests[1]["escalation"]["attempt"] == 2

            # 达到上限后继续巡检不再发
            far = datetime.now(timezone.utc) + timedelta(hours=2)
            assert await sla_service.sweep_escalations(factory, now=far) == 0
            assert len(receiver.requests) == 2

            # 网关重启：同一 SQLite 文件新建引擎，已升级步数不会重发
            engine2 = await create_test_engine(tmp_path, "sla.db", reset=False)
            factory2 = async_sessionmaker(engine2, expire_on_commit=False)
            assert await sla_service.sweep_escalations(factory2, now=far) == 0
            assert len(receiver.requests) == 2
            await engine2.dispose()

            async with factory() as session:
                deliveries = (
                    await session.execute(select(NotificationDelivery))
                ).scalars().all()
                assert [row.kind for row in deliveries] == ["escalation", "escalation"]
                stored = await session.get(DeviceAlert, alert.id)
                assert stored.status == "open", "达上限后仍保持已超时/未处理状态"

        _run(tmp_path, scenario)
    finally:
        receiver.close()


def test_handover_assigns_audits_and_sends_one_summary(tmp_path):
    receiver = _Receiver()
    try:
        async def scenario(client, factory):
            await _seed_notification(factory, receiver.url)
            async with factory() as session:
                session.add(_alert("a-unassigned", created_minutes_ago=40, assignee=None))
                session.add(_alert("a-alice", created_minutes_ago=10, assignee="alice", escalation_count=1))
                session.add(_alert("a-bob", created_minutes_ago=5, assignee="bob"))
                await session.commit()

            resp = await client.post(
                "/api/alerts/handover",
                json={"to": "next-shift", "from_user": "alice", "note": "夜班交接"},
                headers=_headers(ROLE_OPERATOR, "ops"),
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()["data"]
            assert data["count"] == 2
            assert data["window_start"] and data["window_end"]

            # 汇总通知：一条 delivery、接收端只收到一条 alert.handover
            for _ in range(20):
                if receiver.requests:
                    break
                await asyncio.sleep(0.05)
            if not receiver.requests:
                async with factory() as session:
                    rows = (await session.execute(select(NotificationDelivery))).scalars().all()
                    diag = [(row.channel, row.status, row.kind, row.error) for row in rows]
            else:
                diag = []
            assert len(receiver.requests) == 1, f"交接必须只发一条汇总，而不是每条告警一条: {diag}"
            payload = receiver.requests[0]
            assert payload["event"] == "alert.handover"
            assert payload["handover"]["count"] == 2
            assert payload["handover"]["fromUser"] == "alice"
            assert payload["handover"]["to"] == "next-shift"

            async with factory() as session:
                stored = {
                    row.alert_id: row
                    for row in (await session.execute(select(DeviceAlert))).scalars().all()
                }
                assert stored["a-unassigned"].assignee == "next-shift"
                assert stored["a-alice"].assignee == "next-shift"
                assert stored["a-bob"].assignee == "bob", "只交接本班责任人，不越权搬别人的告警"
                # 交接不重置计时与升级计数
                assert stored["a-alice"].escalation_count == 1
                assert stored["a-alice"].sla_deadline_at is not None

                audit = (
                    await session.execute(
                        select(AuditLog).where(AuditLog.action == "alert.handover")
                    )
                ).scalars().all()
                assert len(audit) == 1
                assert audit[0].detail["from"] == "alice"
                assert audit[0].detail["to"] == "next-shift"
                assert audit[0].detail["count"] == 2
                assert audit[0].detail["window_start"] and audit[0].detail["window_end"]

                rows = (await session.execute(select(NotificationDelivery))).scalars().all()
                assert [row.kind for row in rows] == ["handover"]

            # viewer 不允许交接
            resp = await client.post(
                "/api/alerts/handover",
                json={"to": "next-shift"},
                headers=_headers(ROLE_VIEWER, "viewer"),
            )
            assert resp.status_code == 403

        _run(tmp_path, scenario)
    finally:
        receiver.close()
