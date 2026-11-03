"""MQTT 网关客户端：全项目唯一的设备连接消费入口。

职责边界：
- 只做「连接、订阅、分发、发布」，不含任何业务判断；
- 业务处理全部在 app/mqtt/handlers.py 与 services 层，便于单元测试；
- 断线自动指数退避重连，Broker 重启后网关自愈。
"""

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

import aiomqtt

from app.config import get_settings
from app.topics import (
    TOPIC_SYSTEM_ALERT,
    TOPIC_ACK_WILDCARD,
    TOPIC_REPORT_WILDCARD,
    TOPIC_STATUS_WILDCARD,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# 分发回调签名：handler(kind, device_id, payload)
MessageHandlerCallback = Callable[[str, str, Dict[str, Any]], Awaitable[None]]


class MqttGatewayClient:
    """异步 MQTT 客户端封装。

    使用单个长连接同时承担订阅与发布：
    - 订阅：设备上报、状态、回执三类 Topic；
    - 发布：云端指令、系统告警。
    业务层不直接持有 aiomqtt 对象，避免连接细节泄漏到业务代码。
    """

    def __init__(self, handler: MessageHandlerCallback) -> None:
        self._handler = handler
        self._client: Optional[aiomqtt.Client] = None
        self._connected = asyncio.Event()
        self._publish_lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self.last_error: Optional[str] = None
        self.message_count = 0

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        """后台启动消费协程，不阻塞 FastAPI 启动流程。"""
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever(), name="mqtt-consumer")

    async def stop(self) -> None:
        self._stopping = True
        self._connected.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("MQTT 消费协程已停止")

    async def _run_forever(self) -> None:
        backoff = 1
        while not self._stopping:
            try:
                will = aiomqtt.Will(
                    topic=settings.mqtt_will_topic,
                    payload=json.dumps({"status": "offline", "source": "gateway"}),
                    qos=1,
                    retain=True,
                )
                async with aiomqtt.Client(
                    hostname=settings.mqtt_host,
                    port=settings.mqtt_port,
                    username=settings.mqtt_username or None,
                    password=settings.mqtt_password or None,
                    identifier=settings.mqtt_client_id,
                    will=will,
                    keepalive=60,
                ) as client:
                    self._client = client
                    self._connected.set()
                    self.last_error = None
                    backoff = 1
                    logger.info("已连接 MQTT Broker %s:%s", settings.mqtt_host, settings.mqtt_port)

                    await client.subscribe(TOPIC_REPORT_WILDCARD, qos=1)
                    await client.subscribe(TOPIC_STATUS_WILDCARD, qos=1)
                    await client.subscribe(TOPIC_ACK_WILDCARD, qos=1)

                    await client.publish(
                        settings.mqtt_will_topic,
                        json.dumps({"status": "online", "source": "gateway"}),
                        qos=1,
                        retain=True,
                    )

                    async for message in client.messages:
                        await self._dispatch(message)

            except aiomqtt.MqttError as exc:
                self.last_error = str(exc)
                logger.warning("MQTT 连接断开: %s，%s 秒后重连", exc, backoff)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 兜底，避免消费协程意外退出导致网关静默
                self.last_error = str(exc)
                logger.exception("MQTT 消费异常: %s", exc)
            finally:
                self._connected.clear()
                self._client = None

            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _dispatch(self, message: aiomqtt.Message) -> None:
        """解析 Topic 并交给业务处理器，单条消息异常不影响后续消费。"""
        from app.topics import parse_topic

        kind, device_id = parse_topic(str(message.topic))
        if kind is None:
            logger.debug("忽略非设备 Topic: %s", message.topic)
            return

        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("消息体非法 JSON topic=%s err=%s", message.topic, exc)
            return

        self.message_count += 1
        try:
            await self._handler(kind, device_id, payload)
        except Exception as exc:
            logger.exception("处理消息失败 topic=%s err=%s", message.topic, exc)

    async def publish(self, topic: str, payload: Dict[str, Any], qos: int = 1, retain: bool = False) -> None:
        """发布消息；未连接时抛 RuntimeError，由 API 层转 503。"""
        client = self._client
        if client is None or not self._connected.is_set():
            raise RuntimeError("MQTT Broker 未连接")
        async with self._publish_lock:
            await client.publish(
                topic, json.dumps(payload, ensure_ascii=False), qos=qos, retain=retain
            )

    async def publish_alert(self, payload: Dict[str, Any]) -> None:
        await self.publish(TOPIC_SYSTEM_ALERT, payload, qos=1)
