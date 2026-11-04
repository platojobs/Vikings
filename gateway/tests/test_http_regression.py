"""HTTP 层回归：告警批量确认权限/部分失败，以及 PATCH 设备在 MQTT 不可用时仍 200。

这些用例复用真实 FastAPI 路由与依赖（只替换数据库会话和 MQTT 发布器），
用于补齐此前只有 service 层与 E2E 覆盖的 PATCH HTTP 行为。
"""

import asyncio
from datetime import datetime, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401  注册 ORM 元数据
from app.database import Base, get_session
from app.main import app
from app.models import AuditLog, Device, DeviceAlert
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
        # P2-1：对外级别收窄为规则引擎真实产出的 warning / critical
        for legacy in ("info", "notice"):
            legacy_resp = await client.get(
                "/api/alerts", params={"level": legacy}, headers=headers
            )
            assert legacy_resp.status_code == 422

    _run(tmp_path, scenario)


def test_bulk_ack_http_partial_failure_and_permissions(tmp_path):
    """operator 批量确认可见部分失败；viewer 403；未登录 401。"""

    async def scenario(client, factory, _mqtt):
        await _seed_device(factory)
        await _seed_alert(factory, "alt-open", status="open")
        await _seed_alert(factory, "alt-acked", status="acked")

        note = "现场已检查，湿度探头需要校准"
        payload = {"alert_ids": ["alt-open", "alt-acked", "alt-missing"], "note": note}
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

        # 处置元数据：本次确认写入操作人/时间/备注；历史 acked 行不被覆盖
        acked_resp = await client.get(
            "/api/alerts", params={"status": "acked"}, headers=_headers(ROLE_VIEWER, "viewer1")
        )
        rows = {row["alert_id"]: row for row in acked_resp.json()["data"]["items"]}
        assert rows["alt-open"]["acked_by"] == "operator1"
        assert rows["alt-open"]["acked_at"] is not None
        assert rows["alt-open"]["ack_note"] == note
        assert rows["alt-acked"]["acked_by"] is None
        assert rows["alt-acked"]["ack_note"] is None

        # 旧客户端/重试不带 note：照常工作，且不覆盖首次处置信息
        retry = await client.post(
            "/api/alerts/bulk-ack",
            json={"alert_ids": ["alt-open"]},
            headers=_headers(ROLE_OPERATOR, "operator2"),
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["data"]["already_acked"] == ["alt-open"]
        after = await client.get(
            "/api/alerts", params={"status": "acked"}, headers=_headers(ROLE_VIEWER, "viewer1")
        )
        row = next(r for r in after.json()["data"]["items"] if r["alert_id"] == "alt-open")
        assert row["acked_by"] == "operator1"
        assert row["ack_note"] == note

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

        # 备注长度上限 500，超限在参数层拒绝
        too_long = await client.post(
            "/api/alerts/bulk-ack",
            json={"alert_ids": ["alt-open"], "note": "x" * 501},
            headers=_headers(ROLE_OPERATOR, "operator1"),
        )
        assert too_long.status_code == 422

    _run(tmp_path, scenario)


def test_single_ack_http_note_and_legacy_body(tmp_path):
    """单条确认：带备注写入处置信息；不传 body 的旧客户端照常工作。"""

    async def scenario(client, factory, _mqtt):
        await _seed_device(factory)
        await _seed_alert(factory, "alt-single", status="open")
        headers = _headers(ROLE_OPERATOR, "operator1")

        # 备注本身可能夹带敏感信息：业务记录保留原文，审计摘要必须脱敏
        raw_note = "已远程重启设备，Authorization: Bearer e2e-secret-token-123"
        resp = await client.post(
            "/api/alerts/alt-single/ack",
            json={"note": raw_note},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["status"] == "acked"
        assert data["acked_by"] == "operator1"
        assert data["acked_at"] is not None
        assert data["ack_note"] == raw_note

        async with factory() as session:
            entry = (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "alert.ack")
                    .order_by(AuditLog.id.desc())
                )
            ).scalars().first()
        assert entry is not None
        assert entry.operator == "operator1"
        summary = entry.detail.get("note_summary") or ""
        assert "e2e-secret-token-123" not in summary, "审计摘要泄漏了 Bearer token"
        assert "[REDACTED]" in summary

        # 旧客户端不传 body：仍 200，且首次处置信息不被覆盖
        await _seed_alert(factory, "alt-legacy", status="open")
        legacy = await client.post("/api/alerts/alt-legacy/ack", headers=headers)
        assert legacy.status_code == 200, legacy.text
        legacy_data = legacy.json()["data"]
        assert legacy_data["status"] == "acked"
        assert legacy_data["acked_by"] == "operator1"
        assert legacy_data["ack_note"] is None

        # 空白备注归一到 None，不产生空字符串
        await _seed_alert(factory, "alt-blank", status="open")
        blank = await client.post(
            "/api/alerts/alt-blank/ack", json={"note": "   "}, headers=headers
        )
        assert blank.status_code == 200
        assert blank.json()["data"]["ack_note"] is None

        missing = await client.post(
            "/api/alerts/alt-missing/ack",
            json={"note": "不存在"},
            headers=headers,
        )
        assert missing.status_code == 404

    _run(tmp_path, scenario)
