"""告警服务单测：筛选 / 分页 / 批量确认（含部分失败与全失败边界）。"""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401  注册 ORM 元数据
from app.database import Base
from app.models import DeviceAlert
from app.services import alert_service


def _run(tmp_path, scenario):
    async def wrapper():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'test.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            await scenario(session)
            await session.commit()
        await engine.dispose()

    asyncio.run(wrapper())


def _add_alert(
    session,
    alert_id: str,
    device_id: str = "dev-a",
    level: str = "warning",
    status: str = "open",
    minutes: int = 0,
) -> None:
    session.add(
        DeviceAlert(
            alert_id=alert_id,
            device_id=device_id,
            level=level,
            metric="temperature",
            value=80.0,
            threshold=75.0,
            message="测试告警",
            ai_generated=False,
            status=status,
            created_at=datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
            + timedelta(minutes=minutes),
        )
    )


def test_list_alerts_filters_and_pagination(tmp_path):
    """筛选条件相互组合，total 为过滤后总数，分页只影响当页条目。"""

    async def scenario(session):
        _add_alert(session, "alt-a1", "dev-a", "warning", "open", minutes=1)
        _add_alert(session, "alt-a2", "dev-a", "critical", "open", minutes=2)
        _add_alert(session, "alt-a3", "dev-a", "warning", "acked", minutes=3)
        _add_alert(session, "alt-b1", "dev-b", "critical", "open", minutes=4)
        _add_alert(session, "alt-b2", "dev-b", "critical", "acked", minutes=5)
        await session.flush()

        rows, total = await alert_service.list_alerts(session, "dev-a", None, None, 1, 20)
        assert total == 3
        assert [row.alert_id for row in rows] == ["alt-a3", "alt-a2", "alt-a1"]

        rows, total = await alert_service.list_alerts(session, None, "critical", "open", 1, 20)
        assert total == 2
        assert {row.alert_id for row in rows} == {"alt-a2", "alt-b1"}

        rows, total = await alert_service.list_alerts(session, None, "warning", None, 2, 1)
        assert total == 2
        assert [row.alert_id for row in rows] == ["alt-a1"]

        # 越界页返回空列表，total 仍是过滤后总数
        rows, total = await alert_service.list_alerts(session, None, None, None, 99, 20)
        assert rows == []
        assert total == 5

    _run(tmp_path, scenario)


def test_list_alerts_default_query_unchanged(tmp_path):
    """回归：不传任何筛选时返回全部告警，按时间倒序。"""

    async def scenario(session):
        _add_alert(session, "alt-1", minutes=0)
        _add_alert(session, "alt-2", minutes=1)
        rows, total = await alert_service.list_alerts(session, None, None, None, 1, 20)
        assert total == 2
        assert [row.alert_id for row in rows] == ["alt-2", "alt-1"]

    _run(tmp_path, scenario)


def test_bulk_ack_partial_failure_is_visible(tmp_path):
    """open / 已确认 / 不存在三类同时返回，已确认幂等、脏 ID 不拖垮整批。"""

    async def scenario(session):
        _add_alert(session, "alt-open", status="open", minutes=1)
        _add_alert(session, "alt-acked", status="acked", minutes=2)
        await session.flush()

        result = await alert_service.bulk_ack_alerts(
            session, ["alt-open", "alt-acked", "alt-missing", "alt-open"]
        )
        # 重复 ID 去重后 requested 为 3
        assert result["requested"] == 3
        assert result["acked"] == ["alt-open"]
        assert result["already_acked"] == ["alt-acked"]
        assert result["not_found"] == ["alt-missing"]

        rows, total = await alert_service.list_alerts(session, None, None, "open", 1, 20)
        assert total == 0

    _run(tmp_path, scenario)


def test_bulk_ack_records_disposition_metadata(tmp_path):
    """批量确认写入 acked_by / acked_at / ack_note，且已确认行不被改写。"""

    async def scenario(session):
        _add_alert(session, "alt-open-1", status="open", minutes=1)
        _add_alert(session, "alt-open-2", status="open", minutes=2)
        _add_alert(session, "alt-old-acked", status="acked", minutes=3)
        await session.flush()

        result = await alert_service.bulk_ack_alerts(
            session,
            ["alt-open-1", "alt-open-2", "alt-old-acked"],
            operator="alice",
            note="已联系现场，湿度传感器校准中",
        )
        assert result["acked"] == ["alt-open-1", "alt-open-2"]
        assert result["already_acked"] == ["alt-old-acked"]

        rows, _ = await alert_service.list_alerts(session, None, None, "acked", 1, 20)
        by_id = {row.alert_id: row for row in rows}
        for alert_id in ("alt-open-1", "alt-open-2"):
            assert by_id[alert_id].acked_by == "alice"
            assert by_id[alert_id].acked_at is not None
            assert by_id[alert_id].ack_note == "已联系现场，湿度传感器校准中"
        # 历史已确认行保持 NULL，不被本次批量备注覆盖
        assert by_id["alt-old-acked"].acked_by is None
        assert by_id["alt-old-acked"].acked_at is None
        assert by_id["alt-old-acked"].ack_note is None

    _run(tmp_path, scenario)


def test_ack_alert_note_and_idempotent(tmp_path):
    """单条确认记录操作人与备注；重复确认不覆盖首次证据。"""

    async def scenario(session):
        _add_alert(session, "alt-single", status="open", minutes=1)
        await session.flush()

        alert = await alert_service.ack_alert(
            session, "alt-single", operator="bob", note="重启设备后恢复"
        )
        assert alert is not None
        assert alert.status == "acked"
        assert alert.acked_by == "bob"
        assert alert.acked_at is not None
        assert alert.ack_note == "重启设备后恢复"

        # 旧客户端/重复点击：不带备注再次确认，元数据保持第一次的结果
        again = await alert_service.ack_alert(session, "alt-single", operator="carol")
        assert again is not None
        assert again.acked_by == "bob"
        assert again.ack_note == "重启设备后恢复"

    _run(tmp_path, scenario)


def test_ack_alert_missing_returns_none(tmp_path):
    async def scenario(session):
        assert await alert_service.ack_alert(session, "alt-missing") is None

    _run(tmp_path, scenario)


def test_bulk_ack_all_failure_returns_200_semantics(tmp_path):
    """全部失败（都不存在）时不抛异常，逐条返回 not_found。"""

    async def scenario(session):
        result = await alert_service.bulk_ack_alerts(
            session, ["alt-x", "alt-y", "alt-x"]
        )
        assert result["requested"] == 2
        assert result["acked"] == []
        assert result["already_acked"] == []
        assert result["not_found"] == ["alt-x", "alt-y"]

    _run(tmp_path, scenario)


def test_bulk_ack_empty_input_is_noop(tmp_path):
    """空列表（路由层会被 422 拦截）在服务层是幂等空操作。"""

    async def scenario(session):
        result = await alert_service.bulk_ack_alerts(session, [])
        assert result == {
            "acked": [],
            "already_acked": [],
            "not_found": [],
            "requested": 0,
        }

    _run(tmp_path, scenario)
