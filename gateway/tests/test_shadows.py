"""v1.36.0 设备影子测试。

覆盖：
- 影子读写权限（viewer 只读，operator/admin 可写，未登录 401）；
- 期望 merge patch：新增/覆盖/删除键、幂等 patch 不递增版本；
- 同步状态机：empty → pending → synced → offline_pending 的迁移；
- 设备端上报：merge + reported_version 只前进 + 夹紧到 desired 版本；
- 期望键格式校验：非法值类型 / 键过长 / 载荷超限返回 400；
- 批量下发：标签/分组/device_ids 组合取交集，逐设备真实状态；
- 设备删除后影子被清理，不留孤儿行；
- 审计动作写入 detail（键名/版本，不写值）。
"""

import asyncio
from datetime import datetime, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import rules
from app.database import get_session
from app.main import app
from app.models import AuditLog, Device, DeviceShadow, DeviceTag
from app.security import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, create_access_token
from app.services import device_service, shadow_service
from db_utils import create_test_engine


def _headers(role: str, username: str = "tester") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role=role)}"}


def _run(tmp_path, scenario, filename: str = "shadows.db"):
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


async def _seed(session, devices):
    """devices: iterable of (device_id, group_name, status, tags)"""
    for device_id, group, status, tags in devices:
        session.add(Device(device_id=device_id, name=device_id, group_name=group, status=status))
        for tag in tags:
            session.add(DeviceTag(device_id=device_id, tag=tag))
    await session.commit()


# ==================== 基础读写 + 同步状态机 ====================


def test_shadow_lifecycle_and_sync_states(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session, [("dev-1", "默认分组", "online", [])])

        # 1) 初始为空影子：GET 会自动建行，sync_status=empty
        resp = await client.get("/api/devices/dev-1/shadow", headers=_headers(ROLE_VIEWER))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["device_id"] == "dev-1"
        assert data["desired"] == {} and data["reported"] == {}
        assert data["version"] == 0 and data["reported_version"] == 0
        assert data["sync_status"] == "empty"
        assert data["diff_keys"] == []

        # 2) 写入期望：version=1，状态从 empty 变 pending（在线未同步）
        resp = await client.put(
            "/api/devices/dev-1/shadow/desired",
            headers=_headers(ROLE_OPERATOR),
            json={"patch": {"led": "on", "fan_speed": 3}},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["version"] == 1
        assert data["desired"] == {"led": "on", "fan_speed": 3}
        assert data["sync_status"] == "pending"
        assert sorted(data["diff_keys"]) == ["fan_speed", "led"]
        assert data["pending_desired"] == {"led": "on", "fan_speed": 3}

        # 3) 设备上报实际状态：merge 后 synced，reported_version 推进到 1
        resp = await client.post(
            "/api/devices/dev-1/shadow/reported",
            headers=_headers(ROLE_OPERATOR),
            json={"reported": {"led": "on", "fan_speed": 3}, "version": 1},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["sync_status"] == "synced"
        assert data["diff_keys"] == []
        assert data["reported_version"] == 1

        # 4) 设备掉线后再改期望：sync_status=offline_pending
        async with factory() as session:
            device = await session.get(Device, "dev-1")
            device.status = "offline"
            await session.commit()

        resp = await client.put(
            "/api/devices/dev-1/shadow/desired",
            headers=_headers(ROLE_OPERATOR),
            json={"patch": {"fan_speed": 5}},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["version"] == 2
        assert data["sync_status"] == "offline_pending"
        assert data["diff_keys"] == ["fan_speed"]
        assert data["pending_desired"] == {"fan_speed": 5}

        # 5) 设备上线后消费新版本，sync_status 回到 synced
        async with factory() as session:
            device = await session.get(Device, "dev-1")
            device.status = "online"
            await session.commit()
        resp = await client.post(
            "/api/devices/dev-1/shadow/reported",
            headers=_headers(ROLE_OPERATOR),
            json={"reported": {"fan_speed": 5}, "version": 2},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["sync_status"] == "synced"

    _run(tmp_path, scenario)


def test_shadow_patch_idempotent_does_not_bump_version(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session, [("dev-1", "默认分组", "online", [])])

        # 首次写入：version=1
        resp = await client.put(
            "/api/devices/dev-1/shadow/desired",
            headers=_headers(ROLE_OPERATOR),
            json={"patch": {"led": "on"}},
        )
        assert resp.json()["data"]["version"] == 1

        # 同值再写：版本不动
        resp = await client.put(
            "/api/devices/dev-1/shadow/desired",
            headers=_headers(ROLE_OPERATOR),
            json={"patch": {"led": "on"}},
        )
        assert resp.json()["data"]["version"] == 1

        # null 清除已不存在的键：版本也不动
        resp = await client.put(
            "/api/devices/dev-1/shadow/desired",
            headers=_headers(ROLE_OPERATOR),
            json={"patch": {"nonexistent": None}},
        )
        assert resp.json()["data"]["version"] == 1

        # null 清除实际存在的键：版本 +1
        resp = await client.put(
            "/api/devices/dev-1/shadow/desired",
            headers=_headers(ROLE_OPERATOR),
            json={"patch": {"led": None}},
        )
        data = resp.json()["data"]
        assert data["version"] == 2
        assert data["desired"] == {}
        # reported 还没消费删除指令：仍存在 led → diff_keys=['led'] 因为 reported 多了
        # 注意：compute_diff 只看 desired 中的键，desired 已空 → diff_keys=[]，
        # 同步状态回到 empty（reported 残留的 led 视为「无期望的自由状态」）
        assert data["diff_keys"] == []
        assert data["sync_status"] == "empty"

    _run(tmp_path, scenario)


# ==================== 权限与校验 ====================


def test_shadow_write_requires_operator_or_admin(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session, [("dev-1", "默认分组", "online", [])])

        # viewer 写入被拒
        resp = await client.put(
            "/api/devices/dev-1/shadow/desired",
            headers=_headers(ROLE_VIEWER),
            json={"patch": {"led": "on"}},
        )
        assert resp.status_code == 403

        # 未登录 401
        resp = await client.put(
            "/api/devices/dev-1/shadow/desired",
            json={"patch": {"led": "on"}},
        )
        assert resp.status_code == 401

        # admin 可以写
        resp = await client.put(
            "/api/devices/dev-1/shadow/desired",
            headers=_headers(ROLE_ADMIN),
            json={"patch": {"led": "on"}},
        )
        assert resp.status_code == 200

    _run(tmp_path, scenario)


def test_shadow_patch_validation_errors(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session, [("dev-1", "默认分组", "online", [])])

        cases = [
            # 嵌套对象不允许
            ({"patch": {"cfg": {"nested": 1}}}, 400),
            # 数组不允许
            ({"patch": {"cfg": [1, 2, 3]}}, 400),
            # 键超过 32 字符
            ({"patch": {"k" * 33: 1}}, 400),
            # 空 patch 不允许
            ({"patch": {}}, 422),  # pydantic min_length=1
            # 键数量超过 32
            ({"patch": {f"k{i}": i for i in range(33)}}, 422),  # pydantic max_length=32
        ]
        for body, expected_status in cases:
            resp = await client.put(
                "/api/devices/dev-1/shadow/desired",
                headers=_headers(ROLE_OPERATOR),
                json=body,
            )
            assert resp.status_code == expected_status, (body, resp.text)

        # 不存在的设备：404
        resp = await client.put(
            "/api/devices/dev-unknown/shadow/desired",
            headers=_headers(ROLE_OPERATOR),
            json={"patch": {"led": "on"}},
        )
        assert resp.status_code == 404

    _run(tmp_path, scenario)


# ==================== 上报版本号语义 ====================


def test_reported_version_only_advances_and_is_clamped(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session, [("dev-1", "默认分组", "online", [])])

        # 写入 desired v3
        await client.put(
            "/api/devices/dev-1/shadow/desired",
            headers=_headers(ROLE_OPERATOR),
            json={"patch": {"a": 1}},
        )
        await client.put(
            "/api/devices/dev-1/shadow/desired",
            headers=_headers(ROLE_OPERATOR),
            json={"patch": {"b": 2}},
        )
        await client.put(
            "/api/devices/dev-1/shadow/desired",
            headers=_headers(ROLE_OPERATOR),
            json={"patch": {"c": 3}},
        )

        # 设备上报 applied_version=2（落后版本）：reported_version=2
        resp = await client.post(
            "/api/devices/dev-1/shadow/reported",
            headers=_headers(ROLE_OPERATOR),
            json={"reported": {"a": 1, "b": 2}, "version": 2},
        )
        assert resp.json()["data"]["reported_version"] == 2

        # 设备再次上报但 version=1（乱序旧回执）：reported_version 保持 2
        resp = await client.post(
            "/api/devices/dev-1/shadow/reported",
            headers=_headers(ROLE_OPERATOR),
            json={"reported": {"a": 1}, "version": 1},
        )
        assert resp.json()["data"]["reported_version"] == 2

        # 设备上报 applied_version=99（超前）：夹紧到 desired.version=3
        resp = await client.post(
            "/api/devices/dev-1/shadow/reported",
            headers=_headers(ROLE_OPERATOR),
            json={"reported": {"a": 1, "b": 2, "c": 3}, "version": 99},
        )
        data = resp.json()["data"]
        assert data["reported_version"] == 3
        assert data["sync_status"] == "synced"

    _run(tmp_path, scenario)


# ==================== DELETE 端点 ====================


def test_clear_desired_keys_endpoint(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session, [("dev-1", "默认分组", "online", [])])

        await client.put(
            "/api/devices/dev-1/shadow/desired",
            headers=_headers(ROLE_OPERATOR),
            json={"patch": {"a": 1, "b": 2, "c": 3}},
        )

        # 空 keys 拒绝
        resp = await client.delete(
            "/api/devices/dev-1/shadow/desired", headers=_headers(ROLE_OPERATOR)
        )
        assert resp.status_code == 400

        # 清除部分键
        resp = await client.delete(
            "/api/devices/dev-1/shadow/desired?keys=a&keys=b",
            headers=_headers(ROLE_OPERATOR),
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["desired"] == {"c": 3}
        assert data["version"] == 2

    _run(tmp_path, scenario)


# ==================== 批量下发 ====================


def test_batch_desired_with_tag_group_filter(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(
                session,
                [
                    ("dev-a", "华东", "online", ["critical"]),
                    ("dev-b", "华东", "online", ["critical"]),
                    ("dev-c", "华北", "online", ["critical"]),  # 标签同但分组不同
                    ("dev-d", "华东", "online", ["normal"]),  # 分组同但标签不同
                ],
            )

        # 标签 + 分组组合：命中 dev-a、dev-b
        resp = await client.post(
            "/api/devices/shadow/desired/batch",
            headers=_headers(ROLE_OPERATOR),
            json={
                "tags": ["critical"],
                "group": "华东",
                "patch": {"led": "off"},
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["matched"] == 2
        assert data["updated"] == 2
        assert data["unchanged"] == 0
        assert data["not_found"] == 0
        assert {r["device_id"] for r in data["results"]} == {"dev-a", "dev-b"}

        # 同 patch 再下发：unchanged
        resp = await client.post(
            "/api/devices/shadow/desired/batch",
            headers=_headers(ROLE_OPERATOR),
            json={"tags": ["critical"], "group": "华东", "patch": {"led": "off"}},
        )
        data = resp.json()["data"]
        assert data["matched"] == 2
        assert data["updated"] == 0
        assert data["unchanged"] == 2

        # 显式 device_ids + tags 交集
        resp = await client.post(
            "/api/devices/shadow/desired/batch",
            headers=_headers(ROLE_OPERATOR),
            json={
                "device_ids": ["dev-a", "dev-b", "dev-c"],
                "tags": ["critical"],
                "patch": {"fan_speed": 1},
            },
        )
        data = resp.json()["data"]
        # dev-a/dev-b/dev-c 都有 critical 标签 → 3 台都命中
        assert data["matched"] == 3
        assert data["updated"] == 3

        # 显式 ID 被标签条件排除时不进入批次，但不能误报为「设备不存在」
        resp = await client.post(
            "/api/devices/shadow/desired/batch",
            headers=_headers(ROLE_OPERATOR),
            json={
                "device_ids": ["dev-a", "dev-d"],
                "tags": ["critical"],
                "patch": {"fan_speed": 1},
            },
        )
        data = resp.json()["data"]
        assert data["matched"] == 1
        assert data["not_found"] == 0
        assert [r["device_id"] for r in data["results"]] == ["dev-a"]

        # 显式 device_ids 包含不存在的设备：not_found
        resp = await client.post(
            "/api/devices/shadow/desired/batch",
            headers=_headers(ROLE_OPERATOR),
            json={"device_ids": ["dev-a", "dev-ghost"], "patch": {"fan_speed": 2}},
        )
        data = resp.json()["data"]
        assert data["matched"] == 1
        assert data["not_found"] == 1
        statuses = {r["device_id"]: r["status"] for r in data["results"]}
        assert statuses == {"dev-a": "updated", "dev-ghost": "not_found"}

        # 没有任何圈选条件：400
        resp = await client.post(
            "/api/devices/shadow/desired/batch",
            headers=_headers(ROLE_OPERATOR),
            json={"patch": {"led": "on"}},
        )
        assert resp.status_code == 400

    _run(tmp_path, scenario)


# ==================== 审计 ====================


def test_shadow_writes_emit_audit_with_keys_but_not_values(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session, [("dev-1", "默认分组", "online", [])])

        await client.put(
            "/api/devices/dev-1/shadow/desired",
            headers=_headers(ROLE_OPERATOR),
            json={"patch": {"secret_key": "super_secret_value", "fan_speed": 3}},
        )

        async with factory() as session:
            stmt = select(AuditLog).where(AuditLog.action == "device.shadow_desired_update")
            rows = list((await session.execute(stmt)).scalars().all())
            assert len(rows) == 1
            detail = rows[0].detail
            assert detail["target_type" if "target_type" in detail else "mode"] is not None
            assert sorted(detail["keys"]) == ["fan_speed", "secret_key"]
            assert detail["version"] == 1
            # 审计必须不记录值：值可能含设备控制语义或敏感数据
            assert "super_secret_value" not in str(detail)
            assert "3" not in str(detail.get("values", {}))

    _run(tmp_path, scenario)


# ==================== 设备删除清理 ====================


def test_device_delete_cascades_shadow(tmp_path):
    async def scenario(client, factory):
        async with factory() as session:
            await _seed(session, [("dev-1", "默认分组", "online", [])])

        await client.put(
            "/api/devices/dev-1/shadow/desired",
            headers=_headers(ROLE_OPERATOR),
            json={"patch": {"led": "on"}},
        )

        async with factory() as session:
            row = await session.get(DeviceShadow, "dev-1")
            assert row is not None
            ok, err = await device_service.delete_device(session, "dev-1")
            assert ok, err
            await session.commit()
            row = await session.get(DeviceShadow, "dev-1")
            assert row is None

    _run(tmp_path, scenario)


# ==================== 服务层单元：compute_diff / compute_sync_status ====================


def test_compute_diff_and_sync_status_pure_functions():
    # 期望与上报一致 → 无差异
    assert shadow_service.compute_diff({"a": 1}, {"a": 1}) == []
    # 期望有上报无 → 差异
    assert shadow_service.compute_diff({"a": 1}, {}) == ["a"]
    # 值不同 → 差异
    assert shadow_service.compute_diff({"a": 1}, {"a": 2}) == ["a"]
    # reported 多出字段不影响差异（期望只看自己关心的）
    assert shadow_service.compute_diff({"a": 1}, {"a": 1, "extra": 99}) == []

    # 同步状态机
    assert shadow_service.compute_sync_status({}, {}, "online") == "empty"
    assert shadow_service.compute_sync_status({"a": 1}, {"a": 1}, "online") == "synced"
    assert shadow_service.compute_sync_status({"a": 1}, {}, "online") == "pending"
    assert shadow_service.compute_sync_status({"a": 1}, {}, "offline") == "offline_pending"
