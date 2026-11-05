#!/usr/bin/env python3
"""v1.32.0 电池健康与续航看板真实验证（真实 PostgreSQL，不 mock 查询）。

单测跑的是 SQLite；本脚本专门验证 PostgreSQL 分支（date_trunc 按天分桶、
真实 timestamptz 排序、Alembic 空库升级）与聚合口径。

用法（仓库根目录，需本机 PostgreSQL 已启动）：
    gateway/.venv/bin/python scripts/verify_battery_v132.py
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

VERIFY_DB = "iot_battery_verify"


def _repo_database_url() -> str:
    env_file = ROOT / "gateway" / ".env"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("未找到 gateway/.env 的 DATABASE_URL；本脚本只验证真实 PostgreSQL")


def _dsn(url: str, database: str, *, asyncpg: bool) -> str:
    parts = urlsplit(url.replace("postgresql+asyncpg", "postgresql"))
    scheme = "postgresql+asyncpg" if asyncpg else "postgresql"
    return urlunsplit((scheme, parts.netloc, f"/{database}", parts.query, ""))


REPO_URL = _repo_database_url()
VERIFY_URL = _dsn(REPO_URL, VERIFY_DB, asyncpg=True)


def _create_database() -> None:
    owner = urlsplit(REPO_URL.replace("postgresql+asyncpg", "postgresql")).username
    subprocess.run(
        [
            "psql",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f'DROP DATABASE IF EXISTS "{VERIFY_DB}" WITH (FORCE)',
            "-c",
            f'CREATE DATABASE "{VERIFY_DB}" OWNER {owner}',
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _migrate() -> None:
    env = {**os.environ, "DATABASE_URL": VERIFY_URL}
    subprocess.run(
        [str(ROOT / "gateway/.venv/bin/alembic"), "upgrade", "head"],
        cwd=ROOT / "gateway",
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _new_engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(VERIFY_URL, future=True)


def _device(device_id: str, group: str, status: str = "online"):
    from app.models import Device

    return Device(device_id=device_id, name=f"设备 {device_id}", group_name=group, status=status)


def _record(device_id: str, seq: int, battery: int, ts: datetime):
    from app.models import SensorRecord

    return SensorRecord(
        device_id=device_id,
        temperature=25.0,
        humidity=50.0,
        voltage=3.7,
        rssi=-60,
        battery=battery,
        seq=seq,
        ts=ts,
    )


async def _seed(factory) -> int:
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day = lambda offset: today - timedelta(days=offset) + timedelta(hours=1)

    devices = [
        _device("dev-a", "华东"),                 # 电量 10% → 严重
        _device("dev-b", "华东"),                 # 6 天掉 30 点 → 预计 1 天 → 严重
        _device("dev-f", "华东"),                 # 电量 25% → 预警
        _device("dev-c", "华南"),                 # 稳定 95% → 健康
        _device("dev-d", "华南", status="offline"),  # 无任何上报 → 未知
        _device("dev-e", "华南", status="offline"),  # 只有窗口外记录 → 未知
    ]
    records = [
        _record("dev-a", 1, 10, day(0)),
        _record("dev-f", 1, 25, day(0)),
        *[
            _record("dev-b", index + 1, 50 - index * 5, day(6 - index))
            for index in range(7)
        ],
        _record("dev-c", 1, 95, day(2)),
        _record("dev-c", 2, 95, day(1)),
        _record("dev-c", 3, 95, day(0)),
        _record("dev-e", 1, 5, day(8)),
    ]

    async with factory() as session:
        session.add_all(devices + records)
        await session.commit()
    return len(devices) + len(records)


async def main() -> None:
    from app.services.battery_service import battery_health
    from sqlalchemy.ext.asyncio import async_sessionmaker

    _create_database()
    _migrate()
    print(f"空库 Alembic upgrade head: OK（库 {VERIFY_DB}）")

    engine = _new_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        count = await _seed(factory)
        print(f"写入真实数据: 6 台设备 / {count} 条记录（跨 7 天）")

        async with factory() as session:
            fleet = await battery_health(session, days=7, limit=10)
        print("\n整机队聚合:")
        print(
            json.dumps(
                {
                    "device_total": fleet.device_total,
                    "battery_known": fleet.battery_known,
                    "critical": fleet.critical,
                    "warning": fleet.warning,
                    "healthy": fleet.healthy,
                    "unknown": fleet.unknown,
                    "avg_battery": fleet.avg_battery,
                },
                ensure_ascii=False,
            )
        )
        print("风险清单（按紧急度）:")
        for item in fleet.watchlist:
            print(
                f"  {item.device_id}: risk={item.risk} battery={item.battery}% "
                f"drain={item.drain_per_day}%/天 days_to_critical={item.days_to_critical}"
            )
        print("分组汇总:", [(g.group_name, g.total, g.critical, g.warning, g.unknown) for g in fleet.groups])
        print(
            "PG date_trunc 趋势:",
            [(p.day_start.date().isoformat(), p.avg_battery, p.samples) for p in fleet.trend],
        )

        async with factory() as session:
            east = await battery_health(session, group="华东", days=7, limit=10)
        print(f"\n分组过滤 group=华东: total={east.device_total} critical={east.critical} warning={east.warning}")
        print(f"分组趋势末日: {east.trend[-1].avg_battery}%（仅华东设备）")

        async with factory() as session:
            limited = await battery_health(session, days=7, limit=1)
        print(
            f"limit=1: watchlist={[item.device_id for item in limited.watchlist]} "
            f"watchlist_total={limited.watchlist_total}"
        )

        assert fleet.device_total == 6
        assert fleet.battery_known == 4
        assert fleet.critical == 2
        assert fleet.warning == 1
        assert fleet.healthy == 1
        assert fleet.unknown == 2
        assert [item.device_id for item in fleet.watchlist] == ["dev-a", "dev-b", "dev-f"]
        assert east.device_total == 3 and east.warning == 1
        assert len(limited.watchlist) == 1 and limited.watchlist_total == 3
        print("\n全部断言通过：PostgreSQL 分支与聚合口径一致")
    finally:
        await engine.dispose()
        subprocess.run(
            ["psql", "-d", "postgres", "-c", f'DROP DATABASE IF EXISTS "{VERIFY_DB}" WITH (FORCE)'],
            check=False,
            stdout=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    asyncio.run(main())
