"""v1.38.0 告警规则条件树测试。

覆盖：
- 条件树结构校验（未知维度、空叶子、空条件组、超深度、超叶子数）；
- 老字段六种形态与等价条件树的判定结果逐条一致（global / 单值 group /
  单值 device / 多值 group / 多值 tag / 多值 device）；
- 嵌套「(标签A 或 标签B) 且 分组C」通过真实上报链路命中与不命中；
- 条件树写入权限（admin 可写，operator/viewer 403，未登录 401）；
- 条件树变更在审计历史里有字段级 from/to，超长时按策略截断。
"""

import asyncio
import json
from datetime import datetime, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import rule_conditions, rules
from app.database import get_session
from app.main import app
from app.models import AlertRule, Device, DeviceTag
from app.schemas import SensorReportMessage
from app.security import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, create_access_token
from app.services import alert_rule_service, alert_service
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


def _run(tmp_path, scenario, filename: str = "conditions.db"):
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


# ==================== 结构校验 ====================


def test_condition_tree_validation_errors_are_readable():
    bad_cases = [
        ({}, "op"),
        ({"op": "xor", "children": []}, "all"),
        ({"op": "all", "children": []}, "至少"),
        ({"op": "all", "children": [{"dim": "site", "values": ["x"]}]}, "维度"),
        ({"op": "all", "children": [{"dim": "tag", "values": []}]}, "至少"),
        ({"op": "all", "children": [{"dim": "group", "values": ["  "]}]}, "空白"),
        (
            {
                "op": "all",
                "children": [
                    {
                        "op": "any",
                        "children": [
                            {
                                "op": "all",
                                "children": [
                                    {
                                        "op": "any",
                                        "children": [{"dim": "group", "values": ["G"]}],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            "嵌套",
        ),
    ]
    for payload, keyword in bad_cases:
        try:
            rule_conditions.normalize(payload)
        except rule_conditions.ConditionError as exc:
            assert keyword in str(exc), (payload, str(exc))
        else:  # pragma: no cover - 失败时直接暴露
            raise AssertionError(f"应拒绝非法条件树：{payload}")

    too_many = {
        "op": "any",
        "children": [
            {"op": "any", "children": [{"dim": "device", "values": [f"d{i}"]}]}
            for i in range(rule_conditions.MAX_LEAVES + 1)
        ],
    }
    try:
        rule_conditions.normalize(too_many)
    except rule_conditions.ConditionError as exc:
        assert "最多" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应拒绝超过叶子数上限的条件树")


def test_condition_tree_matching_semantics():
    conditions = {
        "op": "all",
        "children": [
            {
                "op": "any",
                "children": [
                    {"dim": "tag", "values": ["cold", "key"]},
                ],
            },
            {"dim": "group", "values": ["华东"]},
        ],
    }
    assert rule_conditions.matches(
        conditions, device_id="d1", group_name="华东", tag_names=["cold"]
    )
    assert rule_conditions.matches(
        conditions, device_id="d2", group_name="华东", tag_names=["other", "key"]
    )
    assert not rule_conditions.matches(
        conditions, device_id="d3", group_name="华北", tag_names=["cold"]
    )
    assert not rule_conditions.matches(
        conditions, device_id="d4", group_name="华东", tag_names=[]
    )
    # 空树/未知结构绝不命中（禁止隐性全局）
    assert not rule_conditions.matches({}, device_id="d1", group_name="华东", tag_names=["cold"])
    assert not rule_conditions.matches(
        {"op": "all"}, device_id="d1", group_name="华东", tag_names=["cold"]
    )


# ==================== 老字段六种形态等价性 ====================


def _legacy_rule(scope_type, scope_value=None, scope_values=None) -> dict:
    return {
        "scope_type": scope_type,
        "scope_value": scope_value,
        "scope_values": scope_values,
        "conditions": None,
    }


def _equivalent_rule(scope_type, scope_value=None, scope_values=None) -> dict:
    """模拟迁移 0013 的回填结果。"""
    values = scope_values or ([scope_value] if scope_value else None)
    conditions = (
        {"op": "any", "children": [{"dim": scope_type, "values": list(values)}]}
        if scope_type != "global" and values
        else None
    )
    return {
        "scope_type": scope_type,
        "scope_value": scope_values[0] if scope_values else scope_value,
        "scope_values": scope_values,
        "conditions": conditions,
    }


def test_legacy_scope_forms_match_identically_after_backfill():
    contexts = [
        {"device_id": "dev-east", "group_name": "华东", "tag_names": ["cold", "key"]},
        {"device_id": "dev-west", "group_name": "华北", "tag_names": ["normal"]},
        {"device_id": "dev-none", "group_name": None, "tag_names": []},
        {"device_id": "dev-key", "group_name": "华东", "tag_names": []},
        {"device_id": "dev-x", "group_name": "华南", "tag_names": ["key"]},
    ]
    forms = [
        ("global", None, None),
        ("group", "华东", None),
        ("device", "dev-east", None),
        ("group", None, ["华东", "华南"]),
        ("tag", None, ["cold", "key"]),
        ("device", None, ["dev-east", "dev-x"]),
    ]
    for scope_type, scope_value, scope_values in forms:
        legacy = _legacy_rule(scope_type, scope_value, scope_values)
        upgraded = _equivalent_rule(scope_type, scope_value, scope_values)
        for ctx in contexts:
            before = rules._in_scope(
                legacy, ctx["device_id"], ctx["group_name"], ctx["tag_names"]
            )
            after = rules._in_scope(
                upgraded, ctx["device_id"], ctx["group_name"], ctx["tag_names"]
            )
            assert before == after, (scope_type, scope_values, ctx)


def test_legacy_mirror_keeps_old_fields_for_downgrade():
    single = rule_conditions.normalize(
        {"op": "any", "children": [{"dim": "tag", "values": ["cold", "key"]}]}
    )
    assert rule_conditions.legacy_mirror(single) == ("tag", "cold", ["cold", "key"])

    nested = rule_conditions.normalize(
        {
            "op": "all",
            "children": [
                {"dim": "tag", "values": ["cold"]},
                {"dim": "group", "values": ["华东"]},
            ],
        }
    )
    assert rule_conditions.legacy_mirror(nested) == ("custom", None, None)


# ==================== 真实上报：嵌套且/或 ====================


async def _seed(session, devices):
    """devices: {device_id: (group_name, tags)}"""
    await alert_rule_service.ensure_default_rules(session)
    for device_id, (group_name, tags) in devices.items():
        session.add(Device(device_id=device_id, name=device_id, group_name=group_name))
        for tag in tags:
            session.add(DeviceTag(device_id=device_id, tag=tag))
    for rule in (
        await session.execute(select(AlertRule).where(AlertRule.is_default.is_(True)))
    ).scalars().all():
        rule.enabled = False
    await session.commit()
    await alert_rule_service.load_rules(session)


def test_nested_condition_rule_hits_via_real_report_path(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(
                session,
                {
                    "dev-a": ("华东", ["cold"]),
                    "dev-b": ("华东", ["key"]),
                    "dev-a2": ("华北", ["cold"]),
                    "dev-c": ("华东", []),
                },
            )

        body = {
            "name": "嵌套条件电压",
            "metric": "voltage",
            "op": "lt",
            "threshold": 3.5,
            "level": "warning",
            "scope_type": "custom",
            "conditions": {
                "op": "all",
                "children": [
                    {
                        "op": "any",
                        "children": [
                            {"dim": "tag", "values": ["cold"]},
                            {"dim": "tag", "values": ["key"]},
                        ],
                    },
                    {"dim": "group", "values": ["华东"]},
                ],
            },
        }
        # 权限：写仅 admin
        assert (await client.post("/api/rules", json=body)).status_code == 401
        assert (
            await client.post("/api/rules", json=body, headers=_headers(ROLE_OPERATOR))
        ).status_code == 403
        assert (
            await client.post("/api/rules", json=body, headers=_headers(ROLE_VIEWER))
        ).status_code == 403

        created = (await client.post("/api/rules", json=body, headers=_headers(ROLE_ADMIN))).json()["data"]
        assert created["scope_type"] == "custom"
        assert created["conditions"]["children"][1]["dim"] == "group"
        assert created["scope_values"] is None, "嵌套条件树无法用单值字段表达"

        async with factory() as session:
            assert await alert_service.handle_report_alerts(
                session, _report("dev-a", voltage=3.3, seq=1), "dev-a", "华东"
            ), "标签 cold + 分组 华东 应命中"
            assert await alert_service.handle_report_alerts(
                session, _report("dev-b", voltage=3.3, seq=1), "dev-b", "华东"
            ), "标签 key + 分组 华东 应命中（组内或）"
            assert not await alert_service.handle_report_alerts(
                session, _report("dev-a2", voltage=3.3, seq=1), "dev-a2", "华北"
            ), "同标签不同分组不得命中（组间且）"
            assert not await alert_service.handle_report_alerts(
                session, _report("dev-c", voltage=3.3, seq=1), "dev-c", "华东"
            ), "无标签设备不得命中"

        # 非法条件树返回 400 且错误可读
        bad = dict(body)
        bad["name"] = "非法条件"
        bad["conditions"] = {"op": "all", "children": [{"dim": "site", "values": ["x"]}]}
        resp = await client.post("/api/rules", json=bad, headers=_headers(ROLE_ADMIN))
        assert resp.status_code == 400 and "维度" in resp.json()["message"]

        # 条件树与老字段冲突：明确报错而不是静默取舍
        conflict = dict(body)
        conflict["name"] = "冲突条件"
        conflict["scope_type"] = "group"
        conflict["scope_values"] = ["华东"]
        resp = await client.post("/api/rules", json=conflict, headers=_headers(ROLE_ADMIN))
        assert resp.status_code == 400 and "scope_type" in resp.json()["message"]

    _run(tmp_path, scenario)


def test_condition_change_appears_in_rule_history(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session, {"dev-a": ("华东", ["cold"]), "dev-b": ("华东", ["key"])})

        created = (await client.post(
            "/api/rules",
            json={
                "name": "历史条件规则", "metric": "voltage", "op": "lt", "threshold": 3.5,
                "level": "warning", "scope_type": "custom",
                "conditions": {"op": "all", "children": [{"dim": "tag", "values": ["cold"]}]},
            },
            headers=_headers(ROLE_ADMIN),
        )).json()["data"]
        rule_id = created["id"]

        resp = await client.put(
            f"/api/rules/{rule_id}",
            json={
                "conditions": {
                    "op": "all",
                    "children": [
                        {"dim": "tag", "values": ["cold"]},
                        {"dim": "group", "values": ["华东"]},
                    ],
                },
            },
            headers=_headers(ROLE_ADMIN),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["scope_type"] == "custom"

        entries = (await client.get(
            f"/api/rules/{rule_id}/history", headers=_headers(ROLE_VIEWER)
        )).json()["data"]
        update = next(entry for entry in entries if entry["action"] == "alert_rule.update")
        change = update["changes"]["conditions"]
        assert change["from"] == {"op": "all", "children": [{"dim": "tag", "values": ["cold"]}]}
        assert change["to"]["children"][1] == {"dim": "group", "values": ["华东"]}
        assert "secret" not in json.dumps(update, ensure_ascii=False)

    _run(tmp_path, scenario)


def test_audit_value_truncates_oversized_condition_tree():
    huge = {
        "op": "any",
        "children": [
            {"dim": "device", "values": [f"device-{i:03d}-" + "x" * 40 for i in range(50)]}
            for _ in range(4)
        ],
    }
    stored = alert_rule_service.audit_value(huge)
    assert stored["truncated"] is True
    assert stored["chars"] > alert_rule_service.AUDIT_VALUE_MAX_CHARS
    assert len(stored["preview"]) == alert_rule_service.AUDIT_VALUE_MAX_CHARS
    # 小对象原样保留，便于历史视图直接展示前后值
    small = {"op": "all", "children": [{"dim": "group", "values": ["华东"]}]}
    assert alert_rule_service.audit_value(small) == small
