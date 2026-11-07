"""v1.35.0 设备标签、规则多值范围与规则变更历史测试。

覆盖：
- 标签写权限（admin 可写，operator/viewer 只读，未登录 401）与格式校验；
- 批量打标签的部分失败语义（not_found / over_limit / unchanged / updated）；
- 标签列表设备计数、设备/告警/导出按标签筛选（多标签为「或」）；
- 不传 tag 时与升级前一致（回归断言）；
- 规则按标签/多分组/多设备命中，规则内多值取「或」；
- v1.34.0 单值规则（scope_value）升级后行为不变；
- 规则变更历史直接来自审计，含前后值。
"""

import asyncio
import csv
import io
from datetime import datetime, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import rules
from app.database import get_session
from app.main import app
from app.models import AlertRule, Device, DeviceAlert, DeviceTag, SensorRecord
from app.schemas import SensorReportMessage
from app.security import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, create_access_token
from app.services import alert_rule_service, alert_service, tag_service
from db_utils import create_test_engine


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


def _run(tmp_path, scenario, filename: str = "tags.db"):
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


async def _seed_devices(session, devices: dict) -> None:
    """devices: {device_id: (group_name, tags)}"""
    await alert_rule_service.ensure_default_rules(session)
    for device_id, (group_name, tags) in devices.items():
        session.add(Device(device_id=device_id, name=device_id, group_name=group_name))
        for tag in tags:
            session.add(DeviceTag(device_id=device_id, tag=tag))
    await session.commit()
    await alert_rule_service.load_rules(session)


def _csv_rows(body: str) -> list[list[str]]:
    return [row for row in csv.reader(io.StringIO(body)) if row]


# ==================== 标签管理：权限、校验、部分失败 ====================


def test_batch_tags_permissions_validation_and_partial_failure(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed_devices(
                session,
                {
                    "dev-a": ("默认分组", []),
                    "dev-b": ("默认分组", []),
                    "dev-full": ("默认分组", [f"t{i}" for i in range(tag_service.TAG_MAX_PER_DEVICE)]),
                },
            )

        url = "/api/devices/tags/batch"
        body = {"device_ids": ["dev-a"], "add": ["east"], "remove": []}
        assert (await client.post(url, json=body)).status_code == 401
        assert (await client.post(url, json=body, headers=_headers(ROLE_OPERATOR))).json()["code"] != 0
        assert (await client.post(url, json=body, headers=_headers(ROLE_VIEWER))).status_code == 403

        # 格式校验：空、纯空白、超长、非法字符、未提供动作
        for bad in ["", "   ", "x" * 34, "坏 标签", "bad,tag"]:
            resp = await client.post(
                url, json={"device_ids": ["dev-a"], "add": [bad], "remove": []},
                headers=_headers(ROLE_ADMIN),
            )
            assert resp.status_code == 400, (bad, resp.text)
            assert resp.json()["message"], "错误必须可读"
        resp = await client.post(
            url, json={"device_ids": ["dev-a"], "add": [], "remove": []},
            headers=_headers(ROLE_ADMIN),
        )
        assert resp.status_code == 400 and "至少" in resp.json()["message"]

        resp = await client.post(
            url,
            json={"device_ids": ["dev-a", "dev-missing", "dev-full"], "add": ["east", "key"], "remove": []},
            headers=_headers(ROLE_ADMIN),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        statuses = {item["device_id"]: item["status"] for item in data["results"]}
        assert statuses == {"dev-a": "updated", "dev-missing": "not_found", "dev-full": "over_limit"}
        assert data["updated"] == 1 and data["not_found"] == 1 and data["over_limit"] == 1
        over = next(item for item in data["results"] if item["device_id"] == "dev-full")
        assert over["tag_count"] == tag_service.TAG_MAX_PER_DEVICE
        assert "上限" in over["error"]

        # 重复添加同一标签按幂等处理：unchanged，不算失败
        resp = await client.post(
            url, json={"device_ids": ["dev-a"], "add": ["east"], "remove": []},
            headers=_headers(ROLE_ADMIN),
        )
        assert resp.json()["data"]["results"][0]["status"] == "unchanged"

        # 移除不存在的标签同样幂等
        resp = await client.post(
            url, json={"device_ids": ["dev-a"], "add": [], "remove": ["never"]},
            headers=_headers(ROLE_ADMIN),
        )
        assert resp.json()["data"]["results"][0]["status"] == "unchanged"

        tags = (await client.get("/api/devices/tags", headers=_headers(ROLE_VIEWER))).json()["data"]
        counts = {item["tag"]: item["device_count"] for item in tags}
        assert counts["east"] == 1 and counts["key"] == 1

    _run(tmp_path, scenario)


# ==================== 标签筛选：设备 / 告警 / 导出 ====================


def test_tag_filters_for_devices_alerts_and_exports(tmp_path):
    async def scenario(client, factory):
        # 导出端点直接使用模块级 SessionFactory（流式响应无法走依赖注入），
        # 测试里替换为当前用例的临时库，避免写到应用默认库。
        import app.api.routes.exports as exports_module

        exports_module.SessionFactory = factory
        async with factory() as session:
            await _seed_devices(
                session,
                {
                    "dev-east": ("华东", ["east", "key"]),
                    "dev-west": ("华北", ["west"]),
                    "dev-none": ("默认分组", []),
                },
            )
            for device_id in ("dev-east", "dev-west", "dev-none"):
                hits = await alert_service.handle_report_alerts(
                    session, _report(device_id, voltage=3.19), device_id, group_name=""
                )
                assert hits, f"{device_id} 应命中默认电压规则"
                session.add(
                    SensorRecord(
                        device_id=device_id, temperature=25, humidity=55, voltage=3.19,
                        rssi=-60, battery=90, seq=1, ts=datetime.now(timezone.utc),
                    )
                )
            await session.commit()

        # 设备列表：单标签
        resp = await client.get(
            "/api/devices", params={"tag": "east"}, headers=_headers(ROLE_VIEWER)
        )
        data = resp.json()["data"]
        assert data["total"] == 1 and [item["device_id"] for item in data["items"]] == ["dev-east"]
        assert data["items"][0]["tags"] == ["east", "key"]

        # 设备列表：多标签为「或」
        resp = await client.get(
            "/api/devices", params=[("tag", "east"), ("tag", "west")],
            headers=_headers(ROLE_VIEWER),
        )
        assert resp.json()["data"]["total"] == 2

        # 不传 tag：与升级前一致（全部 3 台）
        resp = await client.get("/api/devices", headers=_headers(ROLE_VIEWER))
        assert resp.json()["data"]["total"] == 3

        # 标签 + 关键词组合
        resp = await client.get(
            "/api/devices", params={"tag": "east", "keyword": "west"},
            headers=_headers(ROLE_VIEWER),
        )
        assert resp.json()["data"]["total"] == 0

        # 告警列表：按标签筛选
        resp = await client.get(
            "/api/alerts", params={"tag": "east"}, headers=_headers(ROLE_VIEWER)
        )
        assert resp.json()["data"]["total"] == 1
        resp = await client.get(
            "/api/alerts", params=[("tag", "east"), ("tag", "west")],
            headers=_headers(ROLE_VIEWER),
        )
        assert resp.json()["data"]["total"] == 2
        resp = await client.get("/api/alerts", headers=_headers(ROLE_VIEWER))
        assert resp.json()["data"]["total"] == 3

        # 导出：告警 CSV 行数随标签变化
        resp = await client.get(
            "/api/exports/alerts", params={"tag": "east"}, headers=_headers(ROLE_OPERATOR)
        )
        rows = _csv_rows(resp.text)
        assert len(rows) == 2, "表头 + 1 行告警"
        resp = await client.get(
            "/api/exports/alerts", params={"tag": "nope"}, headers=_headers(ROLE_OPERATOR)
        )
        assert len(_csv_rows(resp.text)) == 1, "只有表头"

        # 导出：时序记录 CSV 行数随标签变化
        resp = await client.get(
            "/api/exports/records", params={"tag": "west"}, headers=_headers(ROLE_OPERATOR)
        )
        assert len(_csv_rows(resp.text)) == 2
        resp = await client.get(
            "/api/exports/records", headers=_headers(ROLE_OPERATOR)
        )
        assert len(_csv_rows(resp.text)) == 4

    _run(tmp_path, scenario)


# ==================== 规则范围：标签 / 多值「或」 / 向后兼容 ====================


def test_rule_scope_tag_multi_values_and_legacy_single_value(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed_devices(
                session,
                {
                    "dev-east": ("华东", ["east"]),
                    "dev-north": ("华北", ["north"]),
                    "dev-none": ("默认分组", []),
                },
            )
        # 关闭全部默认规则，避免掩盖自定义范围规则
        async with factory() as session:
            for rule in (await session.execute(
                select(AlertRule).where(AlertRule.is_default.is_(True))
            )).scalars().all():
                rule.enabled = False
            await session.commit()
            await alert_rule_service.load_rules(session)

        created = (await client.post(
            "/api/rules",
            json={
                "name": "标签电压告警", "metric": "voltage", "op": "lt", "threshold": 3.5,
                "level": "warning", "scope_type": "tag", "scope_values": ["east", "key"],
                "priority": 10,
            },
            headers=_headers(ROLE_ADMIN),
        )).json()["data"]
        assert created["scope_values"] == ["east", "key"]
        assert created["scope_value"] == "east", "单值字段保留首个值以兼容旧客户端"

        async with factory() as session:
            # 命中标签 east
            assert await alert_service.handle_report_alerts(
                session, _report("dev-east", voltage=3.3, seq=1), "dev-east", "华东"
            )
            # 非命中标签
            assert not await alert_service.handle_report_alerts(
                session, _report("dev-north", voltage=3.3, seq=1), "dev-north", "华北"
            )
            # 无标签设备
            assert not await alert_service.handle_report_alerts(
                session, _report("dev-none", voltage=3.3, seq=1), "dev-none", "默认分组"
            )
            # 规则内多值为「或」：补上 key 后同一设备命中
            session.add(DeviceTag(device_id="dev-north", tag="key"))
            await session.commit()
            assert await alert_service.handle_report_alerts(
                session, _report("dev-north", voltage=3.3, seq=2), "dev-north", "华北"
            )

        # 多分组「或」
        group_rule = (await client.post(
            "/api/rules",
            json={
                "name": "多分组湿度", "metric": "humidity", "op": "gt", "threshold": 95,
                "level": "warning", "scope_type": "group", "scope_values": ["华东", "华北"],
            },
            headers=_headers(ROLE_ADMIN),
        )).json()["data"]
        assert group_rule["scope_values"] == ["华东", "华北"]

        async with factory() as session:
            assert await alert_service.handle_report_alerts(
                session, _report("dev-north", humidity=96, seq=3), "dev-north", "华北"
            )
            assert not await alert_service.handle_report_alerts(
                session, _report("dev-none", humidity=96, seq=3), "dev-none", "默认分组"
            )

        # 旧客户端单值写法仍可用
        legacy = (await client.post(
            "/api/rules",
            json={
                "name": "单设备电压", "metric": "voltage", "op": "lt", "threshold": 2.9,
                "level": "critical", "scope_type": "device", "scope_value": "dev-none",
            },
            headers=_headers(ROLE_ADMIN),
        )).json()["data"]
        assert legacy["scope_values"] == ["dev-none"]
        async with factory() as session:
            assert await alert_service.handle_report_alerts(
                session, _report("dev-none", voltage=2.8, seq=4), "dev-none", "默认分组"
            )

    _run(tmp_path, scenario)


def test_legacy_rule_row_without_scope_values_still_matches(tmp_path):
    """v1.34.0 单值规则（scope_values 为空）升级后行为不变。"""

    async def scenario(client, factory):
        async with factory() as session:
            await _seed_devices(
                session, {"dev-g1": ("G1", []), "dev-g2": ("G2", [])}
            )
            for rule in (await session.execute(
                select(AlertRule).where(AlertRule.metric == "voltage", AlertRule.is_default.is_(True))
            )).scalars().all():
                rule.enabled = False
            session.add(
                AlertRule(
                    name="旧单值分组规则", metric="voltage", op="lt", threshold=3.5,
                    level="warning", scope_type="group", scope_value="G1", scope_values=None,
                    enabled=True, consecutive_count=1, priority=10,
                )
            )
            await session.commit()
            await alert_rule_service.load_rules(session)
            assert await alert_service.handle_report_alerts(
                session, _report("dev-g1", voltage=3.3, seq=1), "dev-g1", "G1"
            )
            assert not await alert_service.handle_report_alerts(
                session, _report("dev-g2", voltage=3.3, seq=1), "dev-g2", "G2"
            )

    _run(tmp_path, scenario)


# ==================== 规则变更历史 ====================


def test_rule_history_from_audit_with_before_after(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await alert_rule_service.ensure_default_rules(session)
            await alert_rule_service.load_rules(session)

        created = (await client.post(
            "/api/rules",
            json={
                "name": "历史验证规则", "metric": "voltage", "op": "lt", "threshold": 3.5,
                "level": "warning",
            },
            headers=_headers(ROLE_ADMIN),
        )).json()["data"]
        rule_id = created["id"]

        await client.put(
            f"/api/rules/{rule_id}", json={"threshold": 3.3},
            headers=_headers(ROLE_ADMIN),
        )
        await client.patch(
            f"/api/rules/{rule_id}/enabled", json={"enabled": False},
            headers=_headers(ROLE_ADMIN),
        )

        resp = await client.get(
            f"/api/rules/{rule_id}/history", headers=_headers(ROLE_VIEWER)
        )
        assert resp.status_code == 200
        entries = resp.json()["data"]
        actions = [entry["action"] for entry in entries]
        assert "alert_rule.update" in actions and "alert_rule.disable" in actions
        update = next(entry for entry in entries if entry["action"] == "alert_rule.update")
        assert update["changes"]["threshold"] == {"from": 3.5, "to": 3.3}
        disable = next(entry for entry in entries if entry["action"] == "alert_rule.disable")
        assert disable["changes"]["enabled"] == {"from": True, "to": False}
        assert update["operator"] == "tester" and update["created_at"]

        # 未登录 401
        assert (await client.get(f"/api/rules/{rule_id}/history")).status_code == 401

    _run(tmp_path, scenario)
