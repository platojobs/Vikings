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


async def prepare_database() -> None:
    """启动时的数据库准备。

    结构变更不再由 `create_all` + 手写补列驱动（v1.19.0 起全部走 Alembic）：

    - 生产：只校验 `alembic_version` 是否等于代码 head，不一致直接拒绝启动，
      避免「代码新、库旧」的隐性故障；迁移由部署流程显式执行。
    - 开发/测试：自动 `upgrade head`，保留零配置启动体验；已有 SQLite/Postgres
      库由 baseline 迁移自动识别并标记，不会重建表。

    `Base.metadata.create_all` 仅保留给单元测试构造临时库（tests 内部使用），
    不再出现在启动路径。
    """
    from app import migrations

    if settings.is_production:
        await migrations.verify_current(engine)
        return
    await migrations.upgrade_to_head(engine)


async def dispose_engine() -> None:
    await engine.dispose()
