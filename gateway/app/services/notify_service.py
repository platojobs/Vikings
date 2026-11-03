"""告警外推服务：把 critical 告警 POST 到外部 Webhook。

设计约束：
- fire-and-forget：外推失败只记日志，绝不影响 MQTT 消费主链路；
- 独立超时控制，避免外部接口拖慢事件循环；
- 未配置 URL 时整体短路，零开销。
"""

import logging
from typing import Any, Dict

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def send_alert_webhook(alert: Dict[str, Any]) -> None:
    """向配置的 Webhook 推送一条告警。仅 critical 级别触发（调用方负责过滤）。"""
    url = settings.alert_webhook_url
    if not url:
        return

    payload = {
        "msgtype": "text",
        "text": {
            "content": (
                f"[{alert.get('level', '').upper()}] {alert.get('deviceName') or alert.get('deviceId')} "
                f"{alert.get('metric')}={alert.get('value')}（阈值 {alert.get('threshold')}）\n"
                f"{alert.get('message', '')}"
            )
        },
        # 同时附带结构化字段，兼容非钉钉类 Webhook（如飞书自定义机器人、Slack incoming）
        "alert": alert,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.alert_webhook_timeout_seconds) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning("告警 Webhook 返回异常 status=%s body=%s", resp.status_code, resp.text[:200])
            else:
                logger.info("告警 Webhook 推送成功 alertId=%s", alert.get("alertId"))
    except Exception as exc:  # noqa: BLE001 外推失败不允许影响主链路
        logger.warning("告警 Webhook 推送失败 alertId=%s err=%s", alert.get("alertId"), exc)
