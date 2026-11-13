"""Alembic 迁移验收：空库、既有库、显式 stamp、降级往返。

全部使用临时 SQLite 文件（生产 PostgreSQL 由 CI 的 postgres 作业覆盖），
验证「生产已有数据时，迁移不会重建表」这一关键语义。
"""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401  注册元数据
from app.database import Base
from app.migrations import current_revision, upgrade_to_head

GATEWAY_ROOT = Path(__file__).resolve().parents[1]
HEAD = "0012_shadow_history"
# v1.34.0 规则表与命中规则快照列
ALERT_RULE_INDEXES = {"idx_alert_rule_metric", "idx_alert_rule_scope"}
MATCHED_RULE_COLUMN = {"matched_rule"}
NEW_INDEXES = {
    "idx_alert_status_created",
    "idx_alert_level_created",
    "idx_alert_status_device",
    "idx_alert_status_sla_deadline",
    "idx_audit_operator_created",
}
ACK_COLUMNS = {"ack_note", "acked_by", "acked_at"}
# v1.33.0：SLA 快照与值班交接字段
SLA_COLUMNS = {"assignee", "sla_minutes", "sla_deadline_at", "escalation_count", "last_escalated_at"}
DELIVERY_SLA_COLUMNS = {"kind", "extra"}
# v1.31.0：规则表、重试队列字段与索引
DELIVERY_RETRY_COLUMNS = {
    "rule_id",
    "rule_name",
    "group_name",
    "duration_ms",
    "next_attempt_at",
    "finished_at",
    "updated_at",
}


def _run_alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=GATEWAY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _alembic_version(db_path: Path) -> str:
    with sqlite3.connect(db_path) as con:
        rows = con.execute("select version_num from alembic_version").fetchall()
        assert rows, "alembic_version 为空"
        return rows[0][0]


def _index_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as con:
        return {row[0] for row in con.execute("select name from sqlite_master where type='index'")}


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as con:
        return {
            row[0]
            for row in con.execute("select name from sqlite_master where type='table'")
        }


def _alert_columns(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as con:
        return {row[1] for row in con.execute("PRAGMA table_info(device_alert)")}


def _device_columns(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as con:
        return {row[1] for row in con.execute("PRAGMA table_info(device)")}


def _delivery_columns(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as con:
        return {row[1] for row in con.execute("PRAGMA table_info(notification_delivery)")}


def _create_legacy_schema(db_path: Path, *, seed_device: bool = False) -> None:
    """模拟 v1.20 库：表已存在，但缺少 v1.21 的三个处置列、v1.19 索引，也没有 alembic_version。"""

    async def build() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(build())

    with sqlite3.connect(db_path) as con:
        for name in NEW_INDEXES:
            con.execute(f"DROP INDEX {name}")
        # create_all 会带上最新模型列；删掉它们才等价于 v1.33.0 生产库
        for name in ACK_COLUMNS:
            con.execute(f"ALTER TABLE device_alert DROP COLUMN {name}")
        for name in SLA_COLUMNS:
            con.execute(f"ALTER TABLE device_alert DROP COLUMN {name}")
        for name in MATCHED_RULE_COLUMN:
            con.execute(f"ALTER TABLE device_alert DROP COLUMN {name}")
        con.execute("DROP TABLE alert_rule")
        # SQLite 删列会连带删除基于该列的索引（idx_alert_status_sla_deadline）
        con.execute("DROP TABLE sla_config")
        for name in DELIVERY_SLA_COLUMNS:
            con.execute(f"ALTER TABLE notification_delivery DROP COLUMN {name}")
        if seed_device:
            con.execute(
                "INSERT INTO device (device_id, name, model, firmware_version, group_name, status) "
                "VALUES ('dev-legacy', '存量设备', 'VG-S100', '1.0.0', '默认分组', 'online')"
            )
            con.execute(
                "INSERT INTO device_alert "
                "(alert_id, device_id, level, metric, value, threshold, message, ai_generated, status, created_at) "
                "VALUES ('alt-legacy-acked', 'dev-legacy', 'critical', 'voltage', 2.9, 3.2, "
                "'历史告警', 0, 'acked', '2026-11-04 08:00:00')"
            )
        con.commit()


def test_upgrade_on_empty_database(tmp_path):
    db_path = tmp_path / "empty.db"
    result = _run_alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    assert {
        "device",
        "sensor_record",
        "device_alert",
        "device_command",
        "ota_task",
        "rule_config",
        "audit_log",
        "app_user",
        "refresh_token",
        # v1.29.0 通知配置与投递记录
        "notification_config",
        "notification_delivery",
        # v1.31.0 多规则路由
        "notification_rule",
        # v1.33.0 SLA 配置
        "sla_config",
        # v1.34.0 可配置告警规则
        "alert_rule",
        "device_shadow",
        "device_shadow_revision",
    } <= _table_names(db_path)
    assert NEW_INDEXES <= _index_names(db_path)
    assert ALERT_RULE_INDEXES <= _index_names(db_path)
    assert {
        "idx_notify_delivery_status_next",
        "idx_notify_delivery_channel_created",
        "idx_notify_rule_priority",
    } <= _index_names(db_path)
    assert DELIVERY_RETRY_COLUMNS <= _delivery_columns(db_path)
    assert ACK_COLUMNS <= _alert_columns(db_path)
    assert MATCHED_RULE_COLUMN <= _alert_columns(db_path)
    # 空库升级也必须补齐 10 条默认规则，且阈值与 v1.33.0 默认值一致
    with sqlite3.connect(db_path) as con:
        rules = con.execute(
            "select metric, op, level, threshold, scope_type, enabled, consecutive_count "
            "from alert_rule order by metric, priority, id"
        ).fetchall()
    assert len(rules) == 10
    assert rules[0] == ("humidity", "gt", "critical", 97, "global", 1, 1)
    assert ("voltage", "lt", "critical", 3.2, "global", 1, 1) in rules
    assert ("voltage", "lt", "warning", 3.4, "global", 1, 1) in rules
    assert ("temperature", "gt", "critical", 75, "global", 1, 1) in rules
    assert ("rssi", "lt", "critical", -110, "global", 1, 1) in rules
    assert _alembic_version(db_path) == HEAD


def test_existing_database_is_stamped_and_data_preserved(tmp_path):
    """已有库：不能重建表，只记录 baseline，然后继续应用后续 revision。"""
    db_path = tmp_path / "legacy.db"
    _create_legacy_schema(db_path, seed_device=True)

    result = _run_alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    assert "跳过建表" in result.stdout

    with sqlite3.connect(db_path) as con:
        rows = con.execute("select device_id, status from device").fetchall()
        alert_rows = con.execute(
            "select alert_id, status, acked_by, acked_at, ack_note, "
            "assignee, sla_minutes, sla_deadline_at, escalation_count from device_alert"
        ).fetchall()
    assert rows == [("dev-legacy", "online")], "迁移不得清空或重建存量数据"
    assert alert_rows == [("alt-legacy-acked", "acked", None, None, None, None, 30,
                           "2026-11-04 08:30:00", 0)], (
        "历史已确认行保留 acked 与 NULL acked_at；SLA 只按级别回填默认时限，"
        "不臆造责任人（assignee 仍为 NULL），升级计数回填 0"
    )
    assert NEW_INDEXES <= _index_names(db_path)
    assert ACK_COLUMNS <= _alert_columns(db_path)
    assert SLA_COLUMNS <= _alert_columns(db_path)
    assert DELIVERY_SLA_COLUMNS <= _delivery_columns(db_path)
    assert MATCHED_RULE_COLUMN <= _alert_columns(db_path)
    assert ALERT_RULE_INDEXES <= _index_names(db_path)
    # v1.33.0 既有库：默认规则按 v1.33.0 阈值回填，历史告警 matched_rule 保持 NULL
    with sqlite3.connect(db_path) as con:
        rule_count = con.execute("select count(*) from alert_rule where is_default=1").fetchone()[0]
        matched = con.execute(
            "select matched_rule from device_alert where alert_id='alt-legacy-acked'"
        ).fetchone()[0]
    assert rule_count == 10, "既有库升级必须补齐 10 条默认规则"
    assert matched is None, "历史告警不做臆测回填"
    assert _alembic_version(db_path) == HEAD


def test_explicit_stamp_baseline_then_upgrade(tmp_path):
    """运维显式判定路径：先 stamp baseline，再 upgrade。"""
    db_path = tmp_path / "stamped.db"
    _create_legacy_schema(db_path)

    stamp = _run_alembic(db_path, "stamp", "0001_baseline")
    assert stamp.returncode == 0, stamp.stderr
    assert _alembic_version(db_path) == "0001_baseline"

    upgrade = _run_alembic(db_path, "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr
    assert NEW_INDEXES <= _index_names(db_path)
    assert ACK_COLUMNS <= _alert_columns(db_path)
    assert _alembic_version(db_path) == HEAD


def test_downgrade_one_step_then_upgrade(tmp_path):
    db_path = tmp_path / "roundtrip.db"
    assert _run_alembic(db_path, "upgrade", "head").returncode == 0

    # 从 0012 连降七步到 0005，验证影子历史、影子、标签等迁移往返
    down = _run_alembic(db_path, "downgrade", "-7")
    assert down.returncode == 0, down.stderr
    assert _alembic_version(db_path) == "0005_device_cred_revoked"
    assert "device_shadow_revision" not in _table_names(db_path), "downgrade 必须移除影子历史表"
    assert "device_shadow" not in _table_names(db_path), "downgrade 必须移除设备影子表"
    assert "device_tag" not in _table_names(db_path), "downgrade 必须移除设备标签表"
    assert "alert_rule" not in _table_names(db_path), "downgrade 必须移除告警规则表"
    assert not (MATCHED_RULE_COLUMN & _alert_columns(db_path)), "downgrade 必须移除 matched_rule 列"
    assert "notification_config" not in _table_names(db_path), (
        "downgrade 必须移除通知配置表"
    )
    assert "notification_delivery" not in _table_names(db_path), (
        "downgrade 必须移除投递记录表"
    )
    assert "notification_rule" not in _table_names(db_path), (
        "downgrade 必须移除通知规则表"
    )
    assert "sla_config" not in _table_names(db_path), "downgrade 必须移除 SLA 配置表"
    assert not (SLA_COLUMNS & _alert_columns(db_path)), "downgrade 必须移除 SLA 列"
    assert "mqtt_credential_revoked_at" in _device_columns(db_path), (
        "downgrade 不得影响凭证吊销列"
    )
    assert "refresh_token" in _table_names(db_path), "downgrade 不得影响刷新令牌表"
    assert ACK_COLUMNS <= _alert_columns(db_path), "downgrade 不得影响告警处置列"

    up = _run_alembic(db_path, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert ACK_COLUMNS <= _alert_columns(db_path)
    assert MATCHED_RULE_COLUMN <= _alert_columns(db_path)
    assert "refresh_token" in _table_names(db_path)
    assert "notification_config" in _table_names(db_path)
    assert "notification_rule" in _table_names(db_path)
    assert "alert_rule" in _table_names(db_path)
    assert "device_tag" in _table_names(db_path)
    with sqlite3.connect(db_path) as con:
        assert con.execute("select count(*) from alert_rule").fetchone()[0] == 10, (
            "downgrade 再 upgrade 应重新补齐默认规则"
        )
    assert "mqtt_credential_revoked_at" in _device_columns(db_path)
    assert _alembic_version(db_path) == HEAD


def test_v136_shadow_backfilled_as_single_baseline(tmp_path):
    """v1.36 只有当前状态：升级补一个可回滚基线，不伪造更早版本。"""
    db_path = tmp_path / "v136-shadow.db"
    assert _run_alembic(db_path, "upgrade", "0011_device_shadow").returncode == 0
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO device "
            "(device_id, name, model, firmware_version, group_name, status) "
            "VALUES ('dev-v136', '存量影子', 'VG-S100', '1.0.0', '默认分组', 'offline')"
        )
        con.execute(
            "INSERT INTO device_shadow "
            "(device_id, desired, reported, version, desired_updated_by) "
            "VALUES ('dev-v136', '{\"led\":\"on\",\"fan_speed\":2}', '{}', 4, 'ops')"
        )
        con.commit()

    up = _run_alembic(db_path, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    with sqlite3.connect(db_path) as con:
        rows = con.execute(
            "select device_id, version, desired, changed_keys, action, source_version, created_by "
            "from device_shadow_revision"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0:2] == ("dev-v136", 4)
    assert json.loads(rows[0][2]) == {"led": "on", "fan_speed": 2}
    assert json.loads(rows[0][3]) == []
    assert rows[0][4:] == ("baseline", None, "ops")

    down = _run_alembic(db_path, "downgrade", "-1")
    assert down.returncode == 0, down.stderr
    assert "device_shadow_revision" not in _table_names(db_path)
    with sqlite3.connect(db_path) as con:
        current = con.execute(
            "select desired, version from device_shadow where device_id='dev-v136'"
        ).fetchone()
    assert json.loads(current[0]) == {"led": "on", "fan_speed": 2}
    assert current[1] == 4, "downgrade 只删除历史，不影响 v1.36 当前影子"


def test_v129_config_and_delivery_preserved_and_default_rule_seeded(tmp_path):
    """既有 v1.29.0 库：配置与投递历史保留，并回填一条等价默认规则。"""
    db_path = tmp_path / "v129.db"
    assert _run_alembic(db_path, "upgrade", "0006_notifications").returncode == 0
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO notification_config "
            "(id, enabled, webhook_enabled, webhook_url, webhook_secret, smtp_enabled, smtp_to, "
            " levels, groups, device_allow, device_deny) "
            "VALUES (1, 1, 1, 'http://127.0.0.1:9000/hook', 'legacy-secret', 1, 'oncall@example.com', "
            " 'critical', '华东', '', '')"
        )
        con.execute(
            "INSERT INTO notification_delivery "
            "(alert_id, device_id, metric, level, channel, status, attempts, error) "
            "VALUES ('alt-legacy', 'dev-legacy', 'voltage', 'critical', 'webhook', 'sent', 1, NULL)"
        )
        con.commit()

    up = _run_alembic(db_path, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    with sqlite3.connect(db_path) as con:
        config = con.execute(
            "select webhook_url, webhook_secret, smtp_to, updated_by from notification_config"
        ).fetchone()
        rule = con.execute(
            "select name, priority, levels, groups, webhook_enabled, webhook_url, smtp_to "
            "from notification_rule"
        ).fetchall()
        delivery = con.execute(
            "select status, attempts from notification_delivery where alert_id='alt-legacy'"
        ).fetchone()
        delivery_columns = {row[1] for row in con.execute("PRAGMA table_info(notification_delivery)")}
    assert config == ("http://127.0.0.1:9000/hook", "legacy-secret", "oncall@example.com", "system"), (
        "v1.29.0 配置不得被改写"
    )
    assert len(rule) == 1 and rule[0][0].startswith("默认规则"), "应回填一条等价默认规则"
    assert rule[0][1:5] == (100, "critical", "华东", 1)
    assert rule[0][5] == "http://127.0.0.1:9000/hook"
    assert rule[0][6] == "oncall@example.com"
    assert delivery == ("sent", 1), "投递历史必须保留"
    assert DELIVERY_RETRY_COLUMNS <= delivery_columns
    assert _alembic_version(db_path) == HEAD


def test_v133_threshold_overrides_seeded_into_default_rules(tmp_path):
    """v1.33.0 库中在线改过的阈值：升级后必须成为默认规则的低值，行为不回退。"""
    db_path = tmp_path / "v133-override.db"
    assert _run_alembic(db_path, "upgrade", "0008_alert_sla_handover").returncode == 0
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO rule_config (key, value, updated_by) "
            "VALUES ('threshold_voltage_warning', 3.00, 'ops'), "
            "       ('threshold_rssi_critical', -105.00, 'ops')"
        )
        con.commit()

    up = _run_alembic(db_path, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    with sqlite3.connect(db_path) as con:
        rows = con.execute(
            "select metric, level, threshold from alert_rule where is_default=1"
        ).fetchall()
        total = con.execute("select count(*) from alert_rule").fetchone()[0]
    assert total == 10
    assert ("voltage", "warning", 3.0) in rows, "在线覆盖值必须带入默认规则"
    assert ("rssi", "critical", -105.0) in rows
    assert ("voltage", "critical", 3.2) in rows, "未覆盖项保持 v1.33.0 默认值"
    assert ("humidity", "warning", 90.0) in rows


def test_upgrade_to_head_inside_running_loop(tmp_path):
    """覆盖启动路径：在事件循环内复用连接执行迁移（不能再 asyncio.run）。"""
    db_path = tmp_path / "startup.db"

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        await upgrade_to_head(engine)
        assert await current_revision(engine) == HEAD
        await engine.dispose()

    asyncio.run(scenario())
    assert NEW_INDEXES <= _index_names(db_path)
    assert ACK_COLUMNS <= _alert_columns(db_path)
