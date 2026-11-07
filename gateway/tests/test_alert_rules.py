"""可配置告警规则（v1.34.0）。

覆盖：
- 默认规则种子与升级前判定一一对应（同一组样本结果完全相同）；
- 规则 CRUD 权限（admin 可写，operator/viewer 只读，未登录 401）；
- 校验：负数/越界/NaN/反向比较/范围缺值；
- 热生效：改阈值后立即改变判定，无需重启；停用后不再产生新告警；
- 范围隔离：指定分组/指定设备只对本范围生效；
- 多规则裁决：取最高级别，同级按 priority；
- 连续 N 次越限才触发；
- matched_rule 落库与告警详情可解释；规则重载不重复入库；
- 旧 /api/rules/thresholds 契约兼容。
"""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import models, rules  # noqa: F401  注册 ORM 元数据
from app.database import get_session
from app.main import app
from app.models import AlertRule, AuditLog, Device, DeviceAlert
from app.schemas import SensorReportMessage
from app.security import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, create_access_token
from app.services import alert_rule_service, alert_service
from db_utils import create_test_engine


def _headers(role: str, username: str = "tester") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role=role)}"}


def _report(**overrides) -> SensorReportMessage:
    payload = {
        "deviceId": "dev-1",
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


def _run(tmp_path, scenario, filename: str = "rules.db"):
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


async def _seed(session) -> None:
    """测试库由 create_all 建表，需手工补齐默认规则并加载缓存。"""
    await alert_rule_service.ensure_default_rules(session)
    await alert_rule_service.load_rules(session)
    await session.commit()


# ==================== 兼容性：升级前后同一份数据结果一致 ====================


def _legacy_evaluate(report) -> list[dict]:
    """v1.33.0 rules.evaluate 的逐行等价实现（阈值与比较顺序保持原样）。"""
    t = {
        "voltage": {"warning": 3.4, "critical": 3.2},
        "temperature": {"critical_high": 75.0, "warning_high": 60.0,
                        "warning_low": -10.0, "critical_low": -20.0},
        "humidity": {"warning": 90.0, "critical": 97.0},
        "rssi": {"warning": -100, "critical": -110},
    }
    hits = []

    def add(metric, level, value, threshold):
        hits.append({"metric": metric, "level": level, "value": float(value), "threshold": float(threshold)})

    v = float(report.voltage)
    if v < t["voltage"]["critical"]:
        add("voltage", "critical", v, t["voltage"]["critical"])
    elif v < t["voltage"]["warning"]:
        add("voltage", "warning", v, t["voltage"]["warning"])

    temp = float(report.temperature)
    if temp > t["temperature"]["critical_high"]:
        add("temperature", "critical", temp, t["temperature"]["critical_high"])
    elif temp > t["temperature"]["warning_high"]:
        add("temperature", "warning", temp, t["temperature"]["warning_high"])
    elif temp < t["temperature"]["critical_low"]:
        add("temperature", "critical", temp, t["temperature"]["critical_low"])
    elif temp < t["temperature"]["warning_low"]:
        add("temperature", "warning", temp, t["temperature"]["warning_low"])

    h = float(report.humidity)
    if h > t["humidity"]["critical"]:
        add("humidity", "critical", h, t["humidity"]["critical"])
    elif h > t["humidity"]["warning"]:
        add("humidity", "warning", h, t["humidity"]["warning"])

    r = int(report.rssi)
    if r < t["rssi"]["critical"]:
        add("rssi", "critical", r, t["rssi"]["critical"])
    elif r < t["rssi"]["warning"]:
        add("rssi", "warning", r, t["rssi"]["warning"])
    return hits


def _shape(hits):
    return [
        {key: hit[key] for key in ("metric", "level", "value", "threshold")}
        for hit in hits
    ]


def test_upgrade_behavior_matches_v133_samples():
    """升级前后判定对比：临界值、边界值与正常值都逐条一致。"""
    rules.reset_rules_cache()
    rules.set_overrides({})
    baseline = dict(temperature=25.0, humidity=55.0, voltage=3.7, rssi=-60)
    samples = {
        "voltage": [4.5, 3.41, 3.40, 3.39, 3.21, 3.20, 3.19, 2.0],
        "temperature": [-40, -20.01, -20, -19.99, -10, -9.99, 0, 25, 59.99, 60, 60.01, 74.99, 75, 85],
        "humidity": [0, 89.99, 90, 96.99, 97, 100],
        # rssi 上报为整数，样例只用整数避免 legacy int() 截断带来的假差异
        "rssi": [-150, -111, -110, -109, -101, -100, -99, 0],
    }
    compared = 0
    for metric, values in samples.items():
        for value in values:
            report = SimpleNamespace(**{**baseline, metric: value})
            legacy = _shape(_legacy_evaluate(report))
            current = _shape(rules.evaluate(report))
            assert legacy == current, f"{metric}={value} 升级前后不一致: {legacy} != {current}"
            compared += 1
    assert compared == 36


def test_default_rules_seeded_from_rule_config(tmp_path):
    """v1.33.0 在线改过的阈值必须进入默认规则，而不是被环境变量覆盖。"""
    async def scenario(client, factory):
        async with factory() as session:
            session.add(models.RuleConfig(key="threshold_voltage_warning", value=3.0, updated_by="ops"))
            await session.commit()
            seeded = await alert_rule_service.ensure_default_rules(session)
            await alert_rule_service.load_rules(session)
            row = (await session.execute(
                select(AlertRule).where(
                    AlertRule.metric == "voltage", AlertRule.level == "warning", AlertRule.is_default.is_(True)
                )
            )).scalars().one()
            await session.commit()
        assert seeded == 10
        assert float(row.threshold) == 3.0
        assert rules.evaluate(SimpleNamespace(temperature=25.0, humidity=55.0, voltage=3.3, rssi=-60)) == []

    _run(tmp_path, scenario)


# ==================== 权限与校验 ====================


def test_permissions_and_validation(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session)

        assert (await client.get("/api/rules")).status_code == 401

        assert (await client.get("/api/rules", headers=_headers(ROLE_VIEWER, "v"))).status_code == 200
        assert (await client.get("/api/rules", headers=_headers(ROLE_OPERATOR, "o"))).status_code == 200

        body = {
            "name": "电压自定义",
            "metric": "voltage",
            "op": "lt",
            "threshold": 3.35,
            "level": "warning",
            "scope_type": "global",
        }
        assert (await client.post("/api/rules", json=body, headers=_headers(ROLE_OPERATOR, "o"))).status_code == 403
        assert (await client.post("/api/rules", json=body, headers=_headers(ROLE_VIEWER, "v"))).status_code == 403

        invalid_cases = [
            ({**body, "threshold": -3.0}, "2.0~4.5"),
            ({**body, "threshold": 9.0}, "2.0~4.5"),
            ({**body, "metric": "humidity", "op": "lt", "threshold": 50}, "比较方向"),
            ({**body, "metric": "rssi", "op": "gt", "threshold": -100}, "比较方向"),
            ({**body, "scope_type": "group", "scope_value": ""}, "必须填写"),
            ({**body, "scope_type": "device"}, "必须填写"),
            # 反向误配：严重阈值高于警告阈值，会让警告规则永远无法命中
            ({**body, "level": "critical", "threshold": 3.6}, "比较方向疑似反向"),
        ]
        for payload, keyword in invalid_cases:
            resp = await client.post("/api/rules", json=payload, headers=_headers(ROLE_ADMIN, "admin"))
            assert resp.status_code == 400, (payload, resp.text)
            assert keyword in resp.json()["message"], resp.text

        # NaN / 无穷大：服务层拒绝并给可读错误（400），不会进库
        for raw in ("NaN", "Infinity", "-Infinity"):
            resp = await client.post(
                "/api/rules",
                content='{"name":"x","metric":"voltage","op":"lt","threshold":%s,"level":"warning"}'
                % raw,
                headers={**_headers(ROLE_ADMIN, "admin"), "Content-Type": "application/json"},
            )
            assert resp.status_code == 400, resp.text
            assert "有效数字" in resp.json()["message"], resp.text

        resp = await client.post("/api/rules", json=body, headers=_headers(ROLE_ADMIN, "admin"))
        assert resp.status_code == 200, resp.text
        created = resp.json()["data"]
        assert created["is_default"] is False
        assert created["updated_by"] == "admin"

        async with factory() as session:
            audit = (await session.execute(
                select(AuditLog).where(AuditLog.action == "alert_rule.create")
            )).scalars().one()
        assert audit.detail["fields"] == [
            "level", "metric", "name", "op", "scope_type", "threshold",
        ]
        # v1.35.0：规则变更历史需要看到前后值，因此 create 审计记录 changes；
        # 规则字段不含密钥，敏感词仍不得出现。
        assert audit.detail["changes"]["threshold"]["to"] == 3.35
        serialized = json.dumps(audit.detail)
        for secret in ("password", "token", "secret", "Authorization"):
            assert secret not in serialized, f"审计不得记录敏感内容：{secret}"

    _run(tmp_path, scenario)


# ==================== 热生效、范围、裁决、连续次数 ====================


def test_rule_changes_take_effect_without_restart(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session)

        async def report_and_count(device_id: str, voltage: float):
            async with factory() as session:
                session.add(Device(device_id=device_id, name=device_id, group_name="默认分组"))
                await session.flush()
                payloads = await alert_service.handle_report_alerts(
                    session, _report(deviceId=device_id, voltage=voltage), device_id, "默认分组"
                )
                await session.commit()
            return payloads

        first = await report_and_count("dev-hot-1", 2.9)
        assert len(first) == 1 and first[0]["level"] == "critical"
        assert first[0]["matchedRule"]["rule_name"] == "电压过低-严重"
        assert first[0]["matchedRule"]["threshold"] == 3.2

        async with factory() as session:
            rows = (await session.execute(
                select(AlertRule).where(AlertRule.metric == "voltage", AlertRule.is_default.is_(True))
            )).scalars().all()
            ids = {row.level: row.id for row in rows}
        assert set(ids) == {"warning", "critical"}

        # 改阈值：2.9V 不再触发（无需重启，下一次上报立即按新阈值判定）。
        # warning 必须比 critical 宽松，所以两条一起下移，避免 warning 仍命中。
        for level, threshold in (("critical", 2.8), ("warning", 2.9)):
            resp = await client.put(
                f"/api/rules/{ids[level]}",
                json={"threshold": threshold},
                headers=_headers(ROLE_ADMIN, "admin"),
            )
            assert resp.status_code == 200, resp.text
        assert await report_and_count("dev-hot-2", 2.9) == []

        # 改回后恢复触发（恢复为 v1.33.0 默认 3.2 / 3.4；先放宽 warning 再收紧 critical）
        for level, threshold in (("warning", 3.4), ("critical", 3.2)):
            resp = await client.put(
                f"/api/rules/{ids[level]}",
                json={"threshold": threshold},
                headers=_headers(ROLE_ADMIN, "admin"),
            )
            assert resp.status_code == 200, resp.text
        assert len(await report_and_count("dev-hot-3", 2.9)) == 1

    _run(tmp_path, scenario)


def test_scope_group_and_device_isolation(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session)
            session.add(Device(device_id="dev-hd", name="华东设备", group_name="华东"))
            session.add(Device(device_id="dev-hb", name="华北设备", group_name="华北"))
            await session.commit()

        # 停用全局两组电压规则，避免全局规则干扰范围隔离断言
        async with factory() as session:
            for row in (await session.execute(
                select(AlertRule).where(AlertRule.metric == "voltage", AlertRule.is_default.is_(True))
            )).scalars().all():
                row.enabled = False
            await session.commit()

        resp = await client.post(
            "/api/rules",
            json={
                "name": "华东电压专属", "metric": "voltage", "op": "lt", "threshold": 3.5,
                "level": "warning", "scope_type": "group", "scope_value": "华东",
            },
            headers=_headers(ROLE_ADMIN, "admin"),
        )
        assert resp.status_code == 200, resp.text

        resp = await client.post(
            "/api/rules",
            json={
                "name": "单机温度专属", "metric": "temperature", "op": "gt", "threshold": 50,
                "level": "warning", "scope_type": "device", "scope_value": "dev-hd",
            },
            headers=_headers(ROLE_ADMIN, "admin"),
        )
        assert resp.status_code == 200, resp.text

        async def fire(device_id: str, group: str, **overrides):
            async with factory() as session:
                payloads = await alert_service.handle_report_alerts(
                    session, _report(deviceId=device_id, **overrides), device_id, group
                )
                await session.commit()
            return payloads

        hd = await fire("dev-hd", "华东", voltage=3.3, temperature=55)
        assert {item["metric"] for item in hd} == {"voltage", "temperature"}
        assert all(item["matchedRule"]["rule_name"] in ("华东电压专属", "单机温度专属") for item in hd)

        hb = await fire("dev-hb", "华北", voltage=3.3, temperature=55)
        assert hb == [], "分组规则不得影响其它分组"

        other = await fire("dev-other", "默认分组", temperature=55)
        assert other == [], "设备规则不得影响其它设备"

    _run(tmp_path, scenario)


def test_multi_rule_arbitration_prefers_critical_then_priority(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session)

        async def fire(device_id: str, voltage: float):
            async with factory() as session:
                payloads = await alert_service.handle_report_alerts(
                    session, _report(deviceId=device_id, voltage=voltage), device_id, "默认分组"
                )
                await session.commit()
            return payloads

        # 2.9V 同时命中默认 critical(3.2) 与 warning(3.4)，取最高级别 critical
        hits = await fire("dev-arb-1", 2.9)
        assert len(hits) == 1
        assert hits[0]["level"] == "critical"
        assert hits[0]["matchedRule"]["rule_name"] == "电压过低-严重"

        # 同级多条命中：priority 更小者胜（3.3V 同时命中 3.4/3.5 两条 warning）
        resp = await client.post(
            "/api/rules",
            json={
                "name": "电压警告-P50", "metric": "voltage", "op": "lt", "threshold": 3.5,
                "level": "warning", "priority": 50,
            },
            headers=_headers(ROLE_ADMIN, "admin"),
        )
        assert resp.status_code == 200, resp.text
        hits = await fire("dev-arb-2", 3.3)
        assert len(hits) == 1
        assert hits[0]["matchedRule"]["rule_name"] == "电压警告-P50"

    _run(tmp_path, scenario)


def test_consecutive_count_requires_n_breaches(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session)
            session.add(Device(device_id="dev-consec", name="连续设备", group_name="默认分组"))
            await session.commit()

        resp = await client.post(
            "/api/rules",
            json={
                "name": "连续三次湿度", "metric": "humidity", "op": "gt", "threshold": 80,
                "level": "warning", "scope_type": "device", "scope_value": "dev-consec",
                "consecutive_count": 3,
            },
            headers=_headers(ROLE_ADMIN, "admin"),
        )
        assert resp.status_code == 200, resp.text

        async def fire(humidity: float):
            async with factory() as session:
                payloads = await alert_service.handle_report_alerts(
                    session, _report(deviceId="dev-consec", humidity=humidity), "连续设备", "默认分组"
                )
                await session.commit()
            return payloads

        assert await fire(85) == []
        assert await fire(86) == []
        third = await fire(87)
        assert len(third) == 1, "连续第 3 次越限才触发"
        assert third[0]["matchedRule"]["consecutive"] == 3

        # 一次正常上报清零计数，之后两次越限仍不触发
        assert await fire(50) == []
        assert await fire(85) == []
        assert await fire(86) == []

    _run(tmp_path, scenario)


def test_disable_rule_stops_new_alerts_but_history_and_stats_unchanged(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session)
            session.add(Device(device_id="dev-old", name="历史设备", group_name="默认分组"))
            session.add(Device(device_id="dev-new", name="新设备", group_name="默认分组"))
            await session.commit()

        async with factory() as session:
            payloads = await alert_service.handle_report_alerts(
                session, _report(deviceId="dev-old", voltage=2.9), "历史设备", "默认分组"
            )
            await session.commit()
        assert len(payloads) == 1

        async with factory() as session:
            before = int((await session.execute(select(func.count()).select_from(DeviceAlert))).scalar_one())
            summary_before = await alert_service.alert_summary(session)
            for row in (await session.execute(
                select(AlertRule).where(AlertRule.metric == "voltage")
            )).scalars().all():
                await alert_rule_service.set_enabled(session, row.id, False, "admin")
            await session.commit()

        async with factory() as session:
            payloads = await alert_service.handle_report_alerts(
                session, _report(deviceId="dev-new", voltage=2.9), "新设备", "默认分组"
            )
            await session.commit()
            after = int((await session.execute(select(func.count()).select_from(DeviceAlert))).scalar_one())
            summary_after = await alert_service.alert_summary(session)
        assert payloads == [], "规则停用后该指标不再产生新告警"
        assert after == before, "既有告警不受影响"
        assert summary_after == summary_before, "统计口径不受影响"

    _run(tmp_path, scenario)


def test_matched_rule_persisted_and_visible_in_detail(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session)
            session.add(Device(device_id="dev-detail", name="详情设备", group_name="默认分组"))
            await session.commit()

        async with factory() as session:
            await alert_service.handle_report_alerts(
                session, _report(deviceId="dev-detail", temperature=80), "详情设备", "默认分组"
            )
            await session.commit()

        resp = await client.get("/api/alerts?deviceId=dev-detail", headers=_headers(ROLE_VIEWER, "v"))
        assert resp.status_code == 200, resp.text
        item = resp.json()["data"]["items"][0]
        assert item["matched_rule"] == {
            "rule_id": item["matched_rule"]["rule_id"],
            "rule_name": "温度过高-严重",
            "op": "gt",
            "threshold": 75.0,
            "level": "critical",
            "value": 80.0,
            "consecutive": 1,
        }
        assert item["threshold"] == 75.0

    _run(tmp_path, scenario)


def test_rule_reload_does_not_duplicate_alert(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session)
            session.add(Device(device_id="dev-dup", name="重复设备", group_name="默认分组"))
            await session.commit()

        async def fire():
            async with factory() as session:
                payloads = await alert_service.handle_report_alerts(
                    session, _report(deviceId="dev-dup", voltage=2.9), "重复设备", "默认分组"
                )
                await session.commit()
            return payloads

        assert len(await fire()) == 1
        # 规则重载（任意规则写入都会刷新缓存）后同一分钟再上报：抑制窗口内不重复入库
        async with factory() as session:
            await alert_rule_service.load_rules(session)
        assert await fire() == []
        async with factory() as session:
            total = int((await session.execute(
                select(func.count()).select_from(DeviceAlert).where(DeviceAlert.device_id == "dev-dup")
            )).scalar_one())
        assert total == 1

    _run(tmp_path, scenario)


def test_legacy_thresholds_endpoint_still_works(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session)

        resp = await client.put(
            "/api/rules/thresholds",
            json={"voltage_warning": 3.0},
            headers=_headers(ROLE_ADMIN, "admin"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["updated"] == {"voltage_warning": 3.0}

        view = await client.get("/api/rules/thresholds", headers=_headers(ROLE_VIEWER, "v"))
        assert view.json()["data"]["voltage"]["warning"] == {"value": 3.0, "source": "override"}

        async with factory() as session:
            row = (await session.execute(
                select(AlertRule).where(
                    AlertRule.metric == "voltage", AlertRule.level == "warning", AlertRule.is_default.is_(True)
                )
            )).scalars().one()
            stored = float(row.threshold)
            override = await session.get(models.RuleConfig, "threshold_voltage_warning")
        assert stored == 3.0, "旧接口写入必须同步默认规则，重启后不回退"
        assert float(override.value) == 3.0

    _run(tmp_path, scenario)


def test_restore_defaults_rollback(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session)

        async with factory() as session:
            row = (await session.execute(
                select(AlertRule).where(AlertRule.metric == "rssi", AlertRule.level == "critical")
            )).scalars().one()
            row.threshold = -120
            await session.commit()

        resp = await client.post("/api/rules/restore-defaults", headers=_headers(ROLE_ADMIN, "admin"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["restored"] >= 1

        async with factory() as session:
            row = (await session.execute(
                select(AlertRule).where(AlertRule.metric == "rssi", AlertRule.level == "critical")
            )).scalars().one()
            assert float(row.threshold) == -110.0

    _run(tmp_path, scenario)
