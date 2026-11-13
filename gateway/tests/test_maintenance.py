"""v1.39.0 维护窗口：时间语义、拦截链路、汇总与权限。"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import maintenance_window as maintenance, rules
from app.database import get_session
from app.main import app
from app.models import Device, DeviceAlert, DeviceTag, MaintenanceSummary, MaintenanceSuppression
from app.schemas import SensorReportMessage
from app.security import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, create_access_token
from app.services import alert_rule_service, alert_service
from tests.db_utils import create_test_engine


def _headers(role: str, username: str = "tester") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role=role)}"}


def _report(device_id: str, **overrides) -> SensorReportMessage:
    payload = {
        "deviceId": device_id,
        "temperature": 25.0,
        "humidity": 55.0,
        "voltage": 3.7,
        "rssi": -60,
        "battery": 90,
        "seq": 1,
        "ts": int(datetime.now(timezone.utc).timestamp() * 1000),
    }
    payload.update(overrides)
    return SensorReportMessage.model_validate(payload)


def _run(tmp_path, scenario, filename: str = "maintenance.db"):
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
            rules.reset_rules_cache()
            rules.set_overrides({})
            await engine.dispose()

    asyncio.run(wrapper())


async def _seed_devices(session, devices):
    await alert_rule_service.ensure_default_rules(session)
    for device_id, (group_name, tags) in devices.items():
        session.add(Device(device_id=device_id, name=device_id, group_name=group_name))
        for tag in tags:
            session.add(DeviceTag(device_id=device_id, tag=tag))
    await session.commit()
    await alert_rule_service.load_rules(session)


# ==================== 时间语义 ====================


def test_once_window_half_open_boundary():
    start = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)
    window = SimpleNamespace(
        mode="once", status="scheduled", starts_at=start, ends_at=start + timedelta(hours=2)
    )
    assert maintenance.occurrence_for(window, start) is not None
    assert maintenance.occurrence_for(window, start + timedelta(minutes=119)) is not None
    assert maintenance.occurrence_for(window, start + timedelta(hours=2)) is None, "右边界开区间"
    assert maintenance.occurrence_for(window, start - timedelta(seconds=1)) is None


def test_weekly_occurrence_cross_day_and_next_start():
    # 周六 22:00 UTC 起 4 小时 → 跨到周日 02:00
    weekly = SimpleNamespace(
        mode="weekly",
        status="scheduled",
        recurrence={"weekday": 5, "start": "22:00", "duration_minutes": 240},
        starts_at=None,
        ends_at=None,
    )
    crossing = datetime(2026, 11, 14, 23, 30, tzinfo=timezone.utc)  # 周六
    occurrence = maintenance.occurrence_for(weekly, crossing)
    assert occurrence is not None
    assert occurrence.start == datetime(2026, 11, 14, 22, 0, tzinfo=timezone.utc)
    assert occurrence.end == datetime(2026, 11, 15, 2, 0, tzinfo=timezone.utc)

    # 周日凌晨仍命中前一天开始的发生
    assert maintenance.occurrence_for(weekly, datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc))
    # 周日 03:00 已结束
    assert maintenance.occurrence_for(weekly, datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc)) is None


def test_weekly_next_start_cross_week():
    weekly = SimpleNamespace(
        mode="weekly",
        status="scheduled",
        recurrence={"weekday": 6, "start": "18:00", "duration_minutes": 120},
        starts_at=None,
        ends_at=None,
    )
    # 2026-11-13 是周五，下一次应为 2026-11-15（周日）18:00
    nxt = maintenance.next_start(weekly, datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc))
    assert nxt == datetime(2026, 11, 15, 18, 0, tzinfo=timezone.utc)
    # 周日内，下一次是下周
    inside = maintenance.next_start(weekly, datetime(2026, 11, 15, 19, 0, tzinfo=timezone.utc))
    assert inside == datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)


# ==================== 校验与权限 ====================


def test_window_validation_and_permissions(tmp_path):
    async def scenario(client, factory):
        body = {
            "name": "计划维护",
            "reason": "机房割接",
            "scope": {"op": "all", "children": [{"dim": "tag", "values": ["冷库"]}]},
            "mode": "once",
            "starts_at": "2026-11-13T10:00:00Z",
            "ends_at": "2026-11-13T12:00:00Z",
        }
        assert (await client.post("/api/maintenance/windows", json=body)).status_code == 401
        for role in (ROLE_OPERATOR, ROLE_VIEWER):
            assert (
                await client.post("/api/maintenance/windows", json=body, headers=_headers(role))
            ).status_code == 403

        bad = dict(body, ends_at="2026-11-13T09:00:00Z")
        assert (await client.post("/api/maintenance/windows", json=bad, headers=_headers(ROLE_ADMIN))).status_code == 400

        bad_scope = dict(body, scope={"op": "all", "children": []})
        resp = await client.post("/api/maintenance/windows", json=bad_scope, headers=_headers(ROLE_ADMIN))
        assert resp.status_code == 400 and "范围" in resp.json()["message"]

        bad_recurrence = {
            "name": "周期",
            "mode": "weekly",
            "recurrence": {"weekday": 7, "start": "25:00", "duration_minutes": 60},
        }
        assert (
            await client.post(
                "/api/maintenance/windows", json=bad_recurrence, headers=_headers(ROLE_ADMIN)
            )
        ).status_code == 400

        created = await client.post("/api/maintenance/windows", json=body, headers=_headers(ROLE_ADMIN))
        assert created.status_code == 200, created.text
        window_id = created.json()["data"]["window_id"]
        # 只读可以查看
        listed = await client.get("/api/maintenance/windows", headers=_headers(ROLE_VIEWER))
        assert listed.status_code == 200 and listed.json()["data"][0]["window_id"] == window_id
        # 提前结束仅 admin
        assert (
            await client.post(f"/api/maintenance/windows/{window_id}/end", headers=_headers(ROLE_OPERATOR))
        ).status_code == 403
        ended = await client.post(
            f"/api/maintenance/windows/{window_id}/end", headers=_headers(ROLE_ADMIN)
        )
        assert ended.status_code == 200
        assert ended.json()["data"]["status"] == "ended"
        assert ended.json()["data"]["active"] is False

    _run(tmp_path, scenario)


# ==================== 真实上报拦截与汇总 ====================


def test_active_window_suppresses_before_alert_insert(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed_devices(
                session,
                {"dev-cold": ("华东", ["冷库"]), "dev-free": ("华北", [])},
            )
        body = {
            "name": "冷库维护",
            "scope": {"op": "all", "children": [{"dim": "tag", "values": ["冷库"]}]},
            "mode": "once",
            "starts_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "ends_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        }
        created = (await client.post("/api/maintenance/windows", json=body, headers=_headers(ROLE_ADMIN))).json()["data"]
        window_id = created["window_id"]
        assert created["active"] is True

        async with factory() as session:
            suppressed = await alert_service.handle_report_alerts(
                session, _report("dev-cold", voltage=3.0), "dev-cold", group_name="华东"
            )
            await session.commit()
            assert suppressed == [], "窗口内不得返回待发布告警"
            assert (
                await session.execute(
                    select(func.count()).select_from(DeviceAlert).where(DeviceAlert.device_id == "dev-cold")
                )
            ).scalar_one() == 0, "抑制不能先入库再过滤"
            assert (
                await session.execute(
                    select(func.count()).select_from(MaintenanceSuppression).where(
                        MaintenanceSuppression.window_id == window_id
                    )
                )
            ).scalar_one() == 1

            # 范围外设备同一指标仍正常告警
            alerts = await alert_service.handle_report_alerts(
                session, _report("dev-free", voltage=3.0), "dev-free", group_name="华北"
            )
            await session.commit()
            assert alerts and alerts[0]["level"] == "critical"

        listed = await client.get("/api/alerts", headers=_headers(ROLE_VIEWER))
        data = listed.json()["data"]
        total = data["total"] if isinstance(data, dict) else len(data)
        assert total == 1, "被抑制的上报不得进入告警列表/统计"

        # 提前结束 → 立即恢复告警并生成汇总
        ended = await client.post(
            f"/api/maintenance/windows/{window_id}/end", headers=_headers(ROLE_ADMIN)
        )
        assert ended.status_code == 200, ended.text
        summaries = (
            await client.get(
                f"/api/maintenance/windows/{window_id}/summaries", headers=_headers(ROLE_VIEWER)
            )
        ).json()["data"]
        assert len(summaries) == 1
        summary = summaries[0]
        assert summary["suppressed_count"] == 1
        assert summary["critical_suppressed"] == 1
        assert summary["devices"] == ["dev-cold"]
        assert summary["metrics"] == {"voltage": 1}
        assert summary["ended_early"] is True

        async with factory() as session:
            after = await alert_service.handle_report_alerts(
                session, _report("dev-cold", voltage=3.0, seq=2), "dev-cold", group_name="华东"
            )
            await session.commit()
            assert after and after[0]["level"] == "critical", "提前结束后必须立即恢复告警"

    _run(tmp_path, scenario)


def test_sweep_generates_summary_after_window_end(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed_devices(session, {"dev-cold": ("华东", ["冷库"])})
        now = datetime.now(timezone.utc)
        body = {
            "name": "已结束窗口",
            "scope": {"op": "any", "children": [{"dim": "group", "values": ["华东"]}]},
            "mode": "once",
            "starts_at": (now - timedelta(hours=2)).isoformat(),
            "ends_at": (now - timedelta(hours=1)).isoformat(),
        }
        window_id = (
            await client.post("/api/maintenance/windows", json=body, headers=_headers(ROLE_ADMIN))
        ).json()["data"]["window_id"]

        async with factory() as session:
            # 补一条历史抑制记录，模拟窗口期间的上报
            from app.models import MaintenanceWindow
            from app.services import maintenance_service

            row = await maintenance_service.get_window(session, window_id)
            started = maintenance.as_utc(row.starts_at)
            session.add(
                MaintenanceSuppression(
                    window_id=window_id,
                    occurrence_start=started,
                    device_id="dev-cold",
                    metric="temperature",
                    level="warning",
                    value=90,
                    threshold=75,
                    rule_name="高温",
                )
            )
            await session.commit()

        from app.services.maintenance_service import sweep_windows

        created = await sweep_windows(factory, now=datetime.now(timezone.utc))
        assert created == 1
        async with factory() as session:
            summary = (
                await session.execute(
                    select(MaintenanceSummary).where(MaintenanceSummary.window_id == window_id)
                )
            ).scalars().one()
            assert summary.suppressed_count == 1
            assert summary.critical_suppressed == 0
            window = (
                await session.execute(
                    select(MaintenanceWindow).where(MaintenanceWindow.window_id == window_id)
                )
            ).scalars().one()
            assert window.status == "ended", "一次性窗口结束后必须置为 ended"
        # 幂等：再次巡检不重复生成
        assert await sweep_windows(factory, now=datetime.now(timezone.utc)) == 0

    _run(tmp_path, scenario)
