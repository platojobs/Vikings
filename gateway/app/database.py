"""数据库接入层：SQLAlchemy 2.0 异步引擎与会话工厂。"""

import asyncio
import os
import time
from typing import AsyncGenerator, Optional

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings
from app.metrics import (
    DB_QUERY_ERRORS_TOTAL,
    DB_READY,
    DB_READY_CHECK_DURATION_SECONDS,
)


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


@event.listens_for(engine.sync_engine, "handle_error")
def _record_db_error(_exception_context) -> None:  # pragma: no cover - 事件回调
    """所有数据库语句/连接错误计数，供 `/metrics` 与告警规则使用。"""
    DB_QUERY_ERRORS_TOTAL.inc()


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


async def probe_database(
    target: Optional[AsyncEngine] = None, timeout_seconds: float = 3.0
) -> dict:
    """真实读取 `device` 表一行，返回可用性与耗时（供 `/api/health/ready`）。

    不用 `SELECT 1`：后者不触碰库文件/数据块，数据库被独占锁或存储损坏时仍可能通过，
    会让就绪探针变成"只验证连接对象存在"。读取真实表既能验证连接，也能验证 schema 可读。

    超时与异常都视为「数据库不可用」；错误文本经过脱敏，避免把连接串写进响应。
    SQLite 下同样会打开连接并执行查询，因此开发环境也能验证这条链路。
    """
    from app.observability import redact_text

    target_engine = target or engine
    started = time.perf_counter()

    async def _query() -> None:
        async with target_engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM device LIMIT 1"))

    try:
        await asyncio.wait_for(_query(), timeout=timeout_seconds)
    except Exception as exc:
        latency = time.perf_counter() - started
        DB_READY.set(0)
        DB_READY_CHECK_DURATION_SECONDS.set(latency)
        return {
            "ok": False,
            "backend": "sqlite" if settings.is_sqlite else "postgresql",
            "latencyMs": round(latency * 1000, 2),
            "error": redact_text(f"{type(exc).__name__}: {exc}"),
        }

    latency = time.perf_counter() - started
    DB_READY.set(1)
    DB_READY_CHECK_DURATION_SECONDS.set(latency)
    return {
        "ok": True,
        "backend": "sqlite" if settings.is_sqlite else "postgresql",
        "latencyMs": round(latency * 1000, 2),
        "error": None,
    }
