"""数据保留清理的手动入口。

定时任务由网关内的 BackgroundScheduler 每小时执行一次；本模块用于运维手动触发
（例如磁盘告警后立即回收空间），与定时任务共用同一个实现：

    docker compose -f infra/docker-compose.prod.yml --env-file infra/prod/.env \
      exec -T gateway python -m app.maintenance purge --dry-run

    docker compose -f infra/docker-compose.prod.yml --env-file infra/prod/.env \
      exec -T gateway python -m app.maintenance purge

本地开发：

    gateway/.venv/bin/python -m app.maintenance purge --dry-run   # 在 gateway/ 目录执行
"""

import argparse
import asyncio
import json
import sys

from app.background_tasks import count_expired_data, purge_expired_data
from app.database import dispose_engine


async def _run(dry_run: bool) -> int:
    try:
        if dry_run:
            result = await count_expired_data()
            print("待清理（仅统计，未删除）：" + json.dumps(result, ensure_ascii=False))
        else:
            result = await purge_expired_data()
            print("已清理：" + json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按保留策略清理 records / 告警 / 审计日志")
    parser.add_argument("action", choices=["purge", "count"], default="purge", nargs="?")
    parser.add_argument("--dry-run", action="store_true", help="只统计不删除")
    args = parser.parse_args(argv)
    if args.action == "count":
        args.dry_run = True
    return asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
