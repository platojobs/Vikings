"""数据库接入层：SQLAlchemy 2.0 异步引擎与会话工厂。"""

import os
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


settings = get_settings()

# SQLite 场景下确保数据目录存在，避免首次启动因目录缺失失败
if settings.is_sqlite:
    os.makedirs("data", exist_ok=True)

# SQLite 不需要连接池；PostgreSQL 使用默认异步连接池
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=not settings.is_sqlite,
    future=True,
)

SessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入：每个请求一个会话，异常自动回滚。"""
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_models() -> None:
    """开发/演示环境自动建表。

    生产环境请使用 infra/postgres/init.sql 显式管理结构变更，
    这里的 create_all 是幂等的，仅补齐缺失的表；已有表的新增列由轻量迁移补齐。
    """
    from app import models  # noqa: F401  确保模型已注册到 Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_columns)


def _migrate_columns(sync_conn) -> None:
    """轻量列迁移：create_all 不会修改已有表，灰度升级时补齐新增字段。

    只处理可空或带默认值的加列，避免老库启动即失败。
    """
    from sqlalchemy import inspect, text

    columns = {col["name"] for col in inspect(sync_conn).get_columns("device")}
    if "group_name" not in columns:
        sync_conn.execute(
            text("ALTER TABLE device ADD COLUMN group_name VARCHAR(64) NOT NULL DEFAULT '默认分组'")
        )


async def dispose_engine() -> None:
    await engine.dispose()
