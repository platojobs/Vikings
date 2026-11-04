"""测试用数据库工厂。

默认使用每个用例独立的临时 SQLite；CI 的 Postgres 作业设置 `TEST_POSTGRES_URL` 后，
同一批用例改为在真实 PostgreSQL 上执行，并在每个用例开始前清空所有表，
以验证 ORM/服务层在两种后端上的行为一致。

只用 `Base.metadata.create_all`（测试构造库），生产结构仍由 Alembic 管理。
"""

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.database import Base

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")


def _truncate_sql() -> str:
    # 依赖顺序无关，CASCADE 处理外键；alembic_version 不在 metadata 中，不会被清空
    names = ", ".join(table.name for table in reversed(Base.metadata.sorted_tables))
    return f"TRUNCATE {names} RESTART IDENTITY CASCADE"


async def create_test_engine(tmp_path, filename: str = "test.db", *, reset: bool = True) -> AsyncEngine:
    """创建一个已建表、可立即使用的测试引擎。

    - 本地/SQLite：始终新建文件，天然隔离，无需清理；
    - PostgreSQL：复用同一库，因此每个用例前 TRUNCATE，保证用例互不污染。
    """
    if TEST_POSTGRES_URL:
        engine = create_async_engine(TEST_POSTGRES_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            if reset:
                await conn.execute(text(_truncate_sql()))
        return engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / filename}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine
