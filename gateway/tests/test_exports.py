"""CSV 导出（v1.30.0）：筛选复用、流式分批、CSV 兼容性、权限、超限与审计。

导出端点自行创建会话（响应返回后仍要继续流式写），因此这里把
`app.api.routes.exports.SessionFactory` 指向测试库，保证断言的是真实查询结果。
"""

import asyncio
import csv
import io
from datetime import datetime, timezone
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import models  # noqa: F401  注册 ORM 元数据
from app.api.routes import exports as exports_route
from app.main import app
from app.models import AuditLog, Device, DeviceAlert, SensorRecord
from app.security import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, create_access_token
from app.services import alert_service
from db_utils import create_test_engine

BOM = "\ufeff"

MESSY_NAME = '华东, "1号" 设备\n(新)'


def _run(tmp_path, scenario):
    async def wrapper():
        engine = await create_test_engine(tmp_path, "export.db")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        previous = exports_route.SessionFactory
        exports_route.SessionFactory = factory
        try:
            await scenario(factory)
        finally:
            exports_route.SessionFactory = previous
            await engine.dispose()

    asyncio.run(wrapper())


def _headers(role: str, username: str = "tester") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role=role)}"}


def _utc(day: int, hour: int = 8) -> datetime:
    return datetime(2026, 11, day, hour, 0, 0, tzinfo=timezone.utc)


async def _seed(factory) -> None:
    async with factory() as session:
        session.add_all(
            [
                Device(device_id="d-1", name=MESSY_NAME, group_name="华东", status="online"),
                Device(device_id="001", name="前导零设备", group_name="华南", status="online"),
            ]
        )
        session.add_all(
            [
                SensorRecord(
                    device_id="d-1",
                    temperature=12.5,
                    humidity=48.25,
                    voltage=3.71,
                    rssi=-72,
                    battery=88,
                    seq=1,
                    ts=_utc(1),
                ),
                SensorRecord(
                    device_id="d-1",
                    temperature=13.25,
                    humidity=49.5,
                    voltage=3.69,
                    rssi=-70,
                    battery=87,
                    seq=2,
                    ts=_utc(2),
                ),
                SensorRecord(
                    device_id="d-1",
                    temperature=14.75,
                    humidity=51.0,
                    voltage=3.68,
                    rssi=-69,
                    battery=86,
                    seq=3,
                    ts=_utc(3),
                ),
                SensorRecord(
                    device_id="001",
                    temperature=15.0,
                    humidity=52.0,
                    voltage=3.66,
                    rssi=-68,
                    battery=85,
                    seq=4,
                    ts=_utc(3, 9),
                ),
            ]
        )
        session.add_all(
            [
                DeviceAlert(
                    alert_id="alt-1",
                    device_id="d-1",
                    level="critical",
                    metric="voltage",
                    value=2.95,
                    threshold=3.2,
                    message="-20°C, 电压过低\n请联系值班",
                    ai_generated=False,
                    status="open",
                    created_at=_utc(1),
                ),
                DeviceAlert(
                    alert_id="alt-2",
                    device_id="d-1",
                    level="warning",
                    metric="temperature",
                    value=61.5,
                    threshold=60.0,
                    message='温度偏高, 备注含 "引号"',
                    ai_generated=True,
                    status="acked",
                    ack_note="已到现场\n复核完成",
                    acked_by="operator",
                    acked_at=_utc(2, 10),
                    created_at=_utc(2),
                ),
                DeviceAlert(
                    alert_id="alt-3",
                    device_id="001",
                    level="critical",
                    metric="voltage",
                    value=2.9,
                    threshold=3.2,
                    message="电压过低",
                    ai_generated=False,
                    status="open",
                    created_at=_utc(3),
                ),
            ]
        )
        await session.commit()


def _parse(body: bytes) -> tuple[list[str], list[list[str]]]:
    text = body.decode("utf-8")
    assert text.startswith(BOM), "缺少 UTF-8 BOM，Excel 双击会乱码"
    rows = list(csv.reader(io.StringIO(text[len(BOM) :])))
    return rows[0], rows[1:]


def _get(client, url, role, **params):
    return client.get(url, params=params, headers=_headers(role, username=f"u-{role}"))


def test_records_export_roundtrip_and_filters(tmp_path):
    async def scenario(factory):
        await _seed(factory)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await _get(client, "/api/exports/records", ROLE_OPERATOR)
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith("text/csv")
            assert "attachment;" in response.headers["content-disposition"]
            assert "cache-control" in {k.lower(): v for k, v in response.headers.items()}

            header, rows = _parse(response.content)
            assert header == [
                "记录ID",
                "设备ID",
                "设备名称",
                "设备分组",
                "温度(°C)",
                "湿度(%)",
                "电压(V)",
                "信号(dBm)",
                "电量(%)",
                "序号",
                "上报时间(UTC)",
            ]
            assert len(rows) == 4
            # 逗号 / 引号 / 换行原样回读；前导零设备 ID 用文本公式保留
            first = rows[0]
            assert first[2] == MESSY_NAME
            assert first[3] == "华东"
            assert '="001"' in rows[3][1]
            assert first[4] == "12.50"
            assert first[10] == "2026-11-01 08:00:00"

            # 时间范围同上限闭区间：11-02 08:00 与 11-03 08:00 都包含
            filtered = await _get(
                client,
                "/api/exports/records",
                ROLE_OPERATOR,
                deviceId="d-1",
                start="2026-11-02T00:00:00Z",
                end="2026-11-03T08:00:00Z",
            )
            _, ranged = _parse(filtered.content)
            assert len(ranged) == 2

            by_device = await _get(
                client, "/api/exports/records", ROLE_OPERATOR, deviceId="001"
            )
            _, owned = _parse(by_device.content)
            assert len(owned) == 1

            # 分组过滤：与列表页同一套 group_name 语义
            by_group = await _get(
                client, "/api/exports/records", ROLE_OPERATOR, group="华南"
            )
            _, grouped = _parse(by_group.content)
            assert len(grouped) == 1

            empty = await _get(
                client,
                "/api/exports/records",
                ROLE_OPERATOR,
                start="2026-12-01T00:00:00Z",
                end="2026-12-02T00:00:00Z",
            )
            assert empty.status_code == 200
            header_only, rows_only = _parse(empty.content)
            assert header_only[0] == "记录ID"
            assert rows_only == []

            bad = await _get(
                client, "/api/exports/records", ROLE_OPERATOR, start="not-a-time"
            )
            assert bad.status_code == 400
            assert bad.json()["message"] == "时间格式非法，请使用 ISO8601 或毫秒时间戳"

    _run(tmp_path, scenario)


def test_alerts_export_semantics_and_filename(tmp_path):
    async def scenario(factory):
        await _seed(factory)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await _get(
                client,
                "/api/exports/alerts",
                ROLE_OPERATOR,
                level="critical",
                status="open",
                start="2026-11-01T00:00:00Z",
                end="2026-11-05T00:00:00Z",
            )
            assert response.status_code == 200, response.text
            disposition = response.headers["content-disposition"]
            assert 'filename="alerts_2026-11-01_2026-11-05.csv"' in disposition

            header, rows = _parse(response.content)
            assert len(rows) == 2
            assert header[4] == "级别"
            assert header[13] == "确认时间(UTC)"
            # 以 "-" 开头的备注加前缀，避免 Excel 当公式执行
            assert rows[0][8].startswith("'-20")
            assert rows[0][8].endswith("请联系值班")
            assert rows[0][9] == "否"
            assert rows[0][14] == "2026-11-01 08:00:00"

            # 已确认告警带确认人/备注，且换行可回读
            acked = await _get(
                client, "/api/exports/alerts", ROLE_OPERATOR, status="acked"
            )
            _, acked_rows = _parse(acked.content)
            assert len(acked_rows) == 1
            assert acked_rows[0][11] == "operator"
            assert acked_rows[0][12] == "已到现场\n复核完成"
            assert acked_rows[0][9] == "是"

            by_device = await _get(
                client, "/api/exports/alerts", ROLE_OPERATOR, deviceId="001"
            )
            _, device_rows = _parse(by_device.content)
            assert len(device_rows) == 1

            by_group = await _get(
                client, "/api/exports/alerts", ROLE_OPERATOR, group="华东"
            )
            _, group_rows = _parse(by_group.content)
            assert len(group_rows) == 2

            # 无时间范围时文件名用 all + 当天 UTC 日期，避免假装覆盖某个区间
            no_range = await _get(client, "/api/exports/alerts", ROLE_OPERATOR)
            today = datetime.now(timezone.utc).date().isoformat()
            assert f'filename="alerts_all_{today}.csv"' in no_range.headers[
                "content-disposition"
            ]

            # 列表查询与导出共用 alert_service.list_alerts：断言同一时间范围结果一致
            async with factory() as session:
                listed, total = await alert_service.list_alerts(
                    session,
                    None,
                    None,
                    None,
                    1,
                    20,
                    start=_utc(2),
                    end=_utc(3),
                )
            assert total == 2
            assert [row.alert_id for row in listed] == ["alt-3", "alt-2"]

            bad_level = await _get(
                client, "/api/exports/alerts", ROLE_OPERATOR, level="info"
            )
            assert bad_level.status_code == 422

    _run(tmp_path, scenario)


def test_export_permissions_matrix(tmp_path):
    async def scenario(factory):
        await _seed(factory)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for role, expected in (
                (ROLE_VIEWER, 403),
                (ROLE_OPERATOR, 200),
                (ROLE_ADMIN, 200),
            ):
                for path in ("/api/exports/records", "/api/exports/alerts"):
                    response = await _get(client, path, role)
                    assert response.status_code == expected, (role, path, response.text)
                    if expected == 403:
                        assert response.json()["message"] == "当前账号无此操作权限"

            anonymous = await client.get("/api/exports/records")
            assert anonymous.status_code == 401
            assert anonymous.json()["message"] == "缺少访问令牌"

    _run(tmp_path, scenario)


def test_export_limit_returns_422(tmp_path, monkeypatch):
    async def scenario(factory):
        await _seed(factory)
        transport = ASGITransport(app=app)
        # 上限调成 2，验证「超过即失败」而不是静默截断
        monkeypatch.setattr(
            exports_route, "get_settings", lambda: SimpleNamespace(export_max_rows=2)
        )
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await _get(client, "/api/exports/records", ROLE_OPERATOR)
            assert response.status_code == 422
            message = response.json()["message"]
            assert "超过上限 2 行" in message
            assert "缩小时间范围" in message

            scoped = await _get(
                client,
                "/api/exports/records",
                ROLE_OPERATOR,
                start="2026-11-03T00:00:00Z",
                end="2026-11-04T00:00:00Z",
            )
            assert scoped.status_code == 200
            _, rows = _parse(scoped.content)
            assert len(rows) == 2

    _run(tmp_path, scenario)


def test_export_audit_records_metadata_only(tmp_path):
    async def scenario(factory):
        await _seed(factory)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await _get(
                client,
                "/api/exports/alerts",
                ROLE_OPERATOR,
                level="critical",
                status="open",
                start="2026-11-01T00:00:00Z",
                end="2026-11-05T00:00:00Z",
            )
            assert response.status_code == 200

        async with factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(AuditLog).where(AuditLog.action == "export.alerts")
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        entry = rows[0]
        assert entry.operator == f"u-{ROLE_OPERATOR}"
        assert entry.target_type == "export"
        assert entry.target_id == "alerts_2026-11-01_2026-11-05.csv"
        detail = entry.detail
        assert detail["rows"] == 2
        assert detail["duration_ms"] >= 0
        assert detail["filters"] == {
            "deviceId": None,
            "group": None,
            "level": "critical",
            "status": "open",
            "start": "2026-11-01T00:00:00+00:00",
            "end": "2026-11-05T00:00:00+00:00",
            "maxRows": 200000,
        }
        # 审计只记录范围与数量，不落任何导出内容
        assert set(detail) == {"filters", "rows", "duration_ms", "filename"}
        assert "电压过低" not in str(entry.detail)

    _run(tmp_path, scenario)
