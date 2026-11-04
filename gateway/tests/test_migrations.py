"""Alembic 迁移验收：空库、既有库、显式 stamp、降级往返。

全部使用临时 SQLite 文件（生产 PostgreSQL 由 CI 的 postgres 作业覆盖），
验证「生产已有数据时，迁移不会重建表」这一关键语义。
"""

import asyncio
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
NEW_INDEXES = {
    "idx_alert_status_created",
    "idx_alert_level_created",
    "idx_alert_status_device",
    "idx_audit_operator_created",
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


def _create_legacy_schema(db_path: Path, *, seed_device: bool = False) -> None:
    """模拟 v1.18 及之前的库：表已存在，但缺少 v1.19 新增索引，也没有 alembic_version。"""

    async def build() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(build())

    with sqlite3.connect(db_path) as con:
        for name in NEW_INDEXES:
            con.execute(f"DROP INDEX {name}")
        if seed_device:
            con.execute(
                "INSERT INTO device (device_id, name, model, firmware_version, group_name, status) "
                "VALUES ('dev-legacy', '存量设备', 'VG-S100', '1.0.0', '默认分组', 'online')"
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
    } <= _table_names(db_path)
    assert NEW_INDEXES <= _index_names(db_path)
    assert _alembic_version(db_path) == "0002_lifecycle_indexes"


def test_existing_database_is_stamped_and_data_preserved(tmp_path):
    """已有库：不能重建表，只记录 baseline，然后继续应用后续 revision。"""
    db_path = tmp_path / "legacy.db"
    _create_legacy_schema(db_path, seed_device=True)

    result = _run_alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    assert "跳过建表" in result.stdout

    with sqlite3.connect(db_path) as con:
        rows = con.execute("select device_id, status from device").fetchall()
    assert rows == [("dev-legacy", "online")], "迁移不得清空或重建存量数据"
    assert NEW_INDEXES <= _index_names(db_path)
    assert _alembic_version(db_path) == "0002_lifecycle_indexes"


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
    assert _alembic_version(db_path) == "0002_lifecycle_indexes"


def test_downgrade_one_step_then_upgrade(tmp_path):
    db_path = tmp_path / "roundtrip.db"
    assert _run_alembic(db_path, "upgrade", "head").returncode == 0

    down = _run_alembic(db_path, "downgrade", "-1")
    assert down.returncode == 0, down.stderr
    assert _alembic_version(db_path) == "0001_baseline"
    assert not (NEW_INDEXES & _index_names(db_path)), "downgrade 必须移除新增索引"

    up = _run_alembic(db_path, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert NEW_INDEXES <= _index_names(db_path)


def test_upgrade_to_head_inside_running_loop(tmp_path):
    """覆盖启动路径：在事件循环内复用连接执行迁移（不能再 asyncio.run）。"""
    db_path = tmp_path / "startup.db"

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        await upgrade_to_head(engine)
        assert await current_revision(engine) == "0002_lifecycle_indexes"
        await engine.dispose()

    asyncio.run(scenario())
    assert NEW_INDEXES <= _index_names(db_path)
