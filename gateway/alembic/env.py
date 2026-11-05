"""Alembic 运行环境（异步驱动）。

设计要点：
1. 数据库地址统一来自 `app.config`（DATABASE_URL 环境变量），仓库只有一处配置；
2. 支持 asyncpg / aiosqlite：CLI 直接运行时自行创建异步引擎；
3. 支持调用方传入既有连接（`config.attributes["connection"]`）：
   网关启动时在已运行的事件循环里执行迁移，不能再 `asyncio.run`，
   由 `app.migrations` 通过 `AsyncConnection.run_sync` 传入同步连接。
"""

import asyncio
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app import models  # noqa: F401  注册全部 ORM 元数据
from app.config import get_settings
from app.database import Base
from app.models import (  # noqa: F401  仅用于让静态检查知道模型已被引用
    AppUser,
    AuditLog,
    Device,
    DeviceAlert,
    DeviceCommand,
    NotificationConfig,
    NotificationDelivery,
    OtaTask,
    RefreshToken,
    RuleConfig,
    SensorRecord,
)

config = context.config

# Alembic 的 fileConfig 会重置根 handler 级别，并默认禁用已有 logger。
# 网关启动时已经配置了 JSON 日志；此处只在「尚未配置根日志」的 CLI 场景启用
# alembic.ini 的默认日志。否则迁移日志会从 JSON 变回文本，并让 app.request 等
# 已存在的 logger 被禁用。
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
settings = get_settings()


def _database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or settings.database_url


def run_migrations_offline() -> None:
    """生成 SQL 脚本模式（alembic upgrade --sql），不建立真实连接。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": _database_url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    # 调用方（网关启动路径）已提供连接时复用，避免在运行中的事件循环里新建 loop
    existing_connection = config.attributes.get("connection")
    if existing_connection is not None:
        _do_run_migrations(existing_connection)
        return
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
