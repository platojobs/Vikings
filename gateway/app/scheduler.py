"""后台任务调度器：在 lifespan 内启动四个循环协程。

不引入 APScheduler 等额外依赖，用 asyncio.sleep 实现固定周期任务，
覆盖中小规模 IoT 场景完全够用；需要分布式调度时再引入 APScheduler/Celery。
"""

import asyncio
import logging

from app.background_tasks import (
    purge_expired_data,
    retry_notification_deliveries,
    sweep_offline_devices,
    sweep_stale_commands,
)
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class BackgroundScheduler:
    """管理离线巡检、指令超时巡检、数据清理与通知重试四个定时任务。"""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._loop("离线巡检", sweep_offline_devices, settings.offline_sweep_interval_seconds)),
            asyncio.create_task(self._loop("指令超时巡检", sweep_stale_commands, settings.command_sweep_interval_seconds)),
            asyncio.create_task(self._loop("数据清理", purge_expired_data, settings.data_purge_interval_seconds)),
            asyncio.create_task(
                self._loop(
                    "通知重试",
                    retry_notification_deliveries,
                    settings.notification_retry_interval_seconds,
                )
            ),
        ]
        logger.info(
            "后台调度器启动：离线巡检 %ds / 指令超时巡检 %ds / 数据清理 %ds / 通知重试 %ds",
            settings.offline_sweep_interval_seconds,
            settings.command_sweep_interval_seconds,
            settings.data_purge_interval_seconds,
            settings.notification_retry_interval_seconds,
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("后台调度器已停止")

    @staticmethod
    async def _loop(name: str, coro_func, interval: int) -> None:
        while True:
            try:
                await coro_func()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("后台任务 %s 执行失败", name)
            await asyncio.sleep(interval)
