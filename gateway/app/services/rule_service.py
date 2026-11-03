"""阈值在线配置服务：rule_config 表与进程内覆盖缓存的同步。

读取路径：rules.evaluate → 覆盖缓存（rule_config）→ 环境变量 → 默认值。
写入路径：PUT 接口 → upsert rule_config 行 → patch 覆盖缓存 → 审计日志。
"""

import logging
from typing import Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import rules
from app.models import RuleConfig
from app.schemas import ThresholdsUpdateRequest

logger = logging.getLogger(__name__)

# API 入参键 → Settings 属性名（持久化时以此属性名为 rule_config.key）
KEY_TO_ATTR = {
    "voltage_warning": "threshold_voltage_warning",
    "voltage_critical": "threshold_voltage_critical",
    "temperature_warning_high": "threshold_temperature_warning_high",
    "temperature_critical_high": "threshold_temperature_critical_high",
    "temperature_warning_low": "threshold_temperature_warning_low",
    "temperature_critical_low": "threshold_temperature_critical_low",
    "humidity_warning": "threshold_humidity_warning",
    "humidity_critical": "threshold_humidity_critical",
    "rssi_warning": "threshold_rssi_warning",
    "rssi_critical": "threshold_rssi_critical",
}


async def load_overrides(session: AsyncSession) -> None:
    """启动时把 rule_config 全量加载进进程内缓存。"""
    rows = (await session.execute(select(RuleConfig))).scalars().all()
    rules.set_overrides({row.key: float(row.value) for row in rows})
    if rows:
        logger.info("加载阈值覆盖 %d 项: %s", len(rows), sorted(r.key for r in rows))


async def update_thresholds(
    session: AsyncSession, req: ThresholdsUpdateRequest, operator: str
) -> Dict[str, float]:
    """更新传入的阈值键：upsert 数据库行并刷新进程内缓存。

    返回本次实际更新的 {api 键: 新值}。
    """
    payload = req.model_dump(exclude_none=True)
    updated: Dict[str, float] = {}

    for api_key, value in payload.items():
        attr = KEY_TO_ATTR[api_key]
        row = await session.get(RuleConfig, attr)
        if row is None:
            row = RuleConfig(key=attr, value=value, updated_by=operator)
            session.add(row)
        else:
            row.value = value
            row.updated_by = operator
        rules.patch_override(attr, value)
        updated[api_key] = value

    await session.flush()
    logger.info("阈值在线更新 operator=%s changes=%s", operator, updated)
    return updated


def effective_view() -> Dict[str, Dict[str, float]]:
    """GET 接口视图：当前生效值（含来源标记 overridden/env）。"""
    overrides = rules.overrides_snapshot()
    snapshot = rules.thresholds_snapshot()

    def entry(group: str, name: str, api_key: str) -> Dict[str, float]:
        value = snapshot[group][name][0]
        attr = KEY_TO_ATTR[api_key]
        return {"value": value, "source": "override" if attr in overrides else "env"}

    return {
        "voltage": {
            "warning": entry("voltage", "warning", "voltage_warning"),
            "critical": entry("voltage", "critical", "voltage_critical"),
        },
        "temperature": {
            "warning_high": entry("temperature", "warning_high", "temperature_warning_high"),
            "critical_high": entry("temperature", "critical_high", "temperature_critical_high"),
            "warning_low": entry("temperature", "warning_low", "temperature_warning_low"),
            "critical_low": entry("temperature", "critical_low", "temperature_critical_low"),
        },
        "humidity": {
            "warning": entry("humidity", "warning", "humidity_warning"),
            "critical": entry("humidity", "critical", "humidity_critical"),
        },
        "rssi": {
            "warning": entry("rssi", "warning", "rssi_warning"),
            "critical": entry("rssi", "critical", "rssi_critical"),
        },
    }
