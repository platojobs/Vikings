"""MQTT 消息业务处理器：把三类设备消息翻译成业务动作。

每条消息独立开启数据库会话，避免长事务占用连接；
处理失败只记录日志，绝不阻塞后续消息（IoT 场景下丢一条可接受，卡死不可接受）。
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from pydantic import ValidationError

from app.database import SessionFactory
from app.mqtt.client import MqttGatewayClient
from app.schemas import CommandAckMessage, DeviceStatusMessage, SensorReportMessage
from app.services import alert_service, command_service, device_service, notify_service

logger = logging.getLogger(__name__)


class MessageHandler:
    """持有 MQTT 客户端引用，用于告警回推；消费入口由 MqttGatewayClient 调用。"""

    def __init__(self) -> None:
        self._mqtt: Optional[MqttGatewayClient] = None

    def bind(self, mqtt: MqttGatewayClient) -> None:
        self._mqtt = mqtt

    async def __call__(self, kind: str, device_id: str, payload: Dict[str, Any]) -> None:
        if kind == "report":
            await self._handle_report(device_id, payload)
        elif kind == "status":
            await self._handle_status(device_id, payload)
        elif kind == "ack":
            await self._handle_ack(device_id, payload)
        else:
            logger.debug("未知消息类型: %s", kind)

    # ---------------- 传感器上报 ----------------
    async def _handle_report(self, device_id: str, payload: Dict[str, Any]) -> None:
        try:
            msg = SensorReportMessage.model_validate(payload)
        except ValidationError as exc:
            logger.warning("上报数据校验失败 device=%s err=%s", device_id, exc.errors()[:2])
            return

        # Topic 里的 deviceId 与报文里的 deviceId 不一致视为伪造/错发，直接丢弃
        if msg.device_id != device_id:
            logger.warning("Topic 与报文设备 ID 不一致 topic=%s payload=%s", device_id, msg.device_id)
            return

        async with SessionFactory() as session:
            inserted = await device_service.save_report(session, msg)
            if not inserted:
                return  # 幂等丢弃，不重复触发告警

            await device_service.touch_report_time(session, msg)
            device = await device_service.ensure_device(session, msg.device_id)
            await session.commit()

            alert_payloads = await alert_service.handle_report_alerts(
                session, msg, device.name, group_name=device.group_name
            )
            await session.commit()

        for alert in alert_payloads:
            if self._mqtt is not None:
                await self._mqtt.publish_alert(alert)
            # 通知是旁路：异步派发，失败/重试都不阻塞 MQTT 消费与告警入库（v1.29.0）
            asyncio.create_task(notify_service.dispatch_alert(alert))

    # ---------------- 上下线状态 ----------------
    async def _handle_status(self, device_id: str, payload: Dict[str, Any]) -> None:
        try:
            msg = DeviceStatusMessage.model_validate(payload)
        except ValidationError as exc:
            logger.warning("状态数据校验失败 device=%s err=%s", device_id, exc.errors()[:2])
            return

        async with SessionFactory() as session:
            await device_service.apply_status(session, msg)
            await session.commit()

        logger.info("设备状态变更 device=%s status=%s reason=%s", device_id, msg.status, msg.reason)

    # ---------------- 指令回执 ----------------
    async def _handle_ack(self, device_id: str, payload: Dict[str, Any]) -> None:
        try:
            msg = CommandAckMessage.model_validate(payload)
        except ValidationError as exc:
            logger.warning("回执校验失败 device=%s err=%s", device_id, exc.errors()[:2])
            return

        async with SessionFactory() as session:
            cmd = await command_service.apply_ack(session, msg, device_id=device_id)
            await session.commit()

        if cmd is None:
            logger.warning("回执对应指令不存在 cmd=%s", msg.cmd_id)
            return
        logger.info(
            "指令回执 cmd=%s device=%s state=%s progress=%s",
            msg.cmd_id, device_id, msg.state, msg.progress,
        )
