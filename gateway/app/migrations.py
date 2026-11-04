"""Alembic 迁移的执行入口。

启动路径与运维 CLI 共用同一套逻辑，避免「启动自动建表」和「迁移文件」两套结构漂移：
- 开发/测试：启动时自动 `upgrade head`，保持零配置可跑；
- 生产：只校验版本，不自动改结构，迁移在部署流程中显式执行（见 docs/deployment.md）；
- CLI：`alembic upgrade head` / `python -m app.migrations current` 均可直接使用。
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

GATEWAY_ROOT = Path(__file__).resolve().parents[1]


def alembic_config() -> Config:
    """仓库内 alembic.ini 的绝对路径配置；URL 始终取当前 Settings。"""
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


async def current_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        return await conn.run_sync(
            lambda sync_conn: MigrationContext.configure(sync_conn).get_current_revision()
        )


async def upgrade_to_head(engine: AsyncEngine) -> None:
    """在已运行的事件循环里执行迁移（复用异步连接，不新建事件循环）。"""
    config = alembic_config()

    async with engine.connect() as conn:
        await conn.run_sync(lambda sync_conn: _upgrade_with_connection(config, sync_conn))


def _upgrade_with_connection(config: Config, sync_conn) -> None:
    config.attributes["connection"] = sync_conn
    command.upgrade(config, "head")


async def verify_current(engine: AsyncEngine) -> None:
    """生产启动前校验：数据库版本必须等于代码 head。"""
    current = await current_revision(engine)
    head = head_revision()
    if current != head:
        raise RuntimeError(
            f"数据库迁移版本不一致（当前 {current or '未迁移'}，代码 {head}）。"
            "生产环境不会自动改表，请先执行迁移：\n"
            "  docker compose -f infra/docker-compose.prod.yml --env-file infra/prod/.env "
            "run --rm migrate\n"
            "或进入容器执行：alembic upgrade head"
        )


async def _async_main(action: str) -> None:
    from app.database import dispose_engine, engine

    if action == "upgrade":
        await upgrade_to_head(engine)
    elif action == "current":
        print(await current_revision(engine) or "未迁移")
    elif action == "verify":
        await verify_current(engine)
        print(f"schema 版本正确：{head_revision()}")
    await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据库迁移（Alembic 包装）")
    parser.add_argument("action", choices=["upgrade", "current", "verify"], default="upgrade", nargs="?")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    asyncio.run(_async_main(args.action))
    return 0


if __name__ == "__main__":
    sys.exit(main())
