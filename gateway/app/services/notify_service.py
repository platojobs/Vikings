"""告警通知（v1.29.0）：Webhook + SMTP，规则过滤、静默窗口、重试与留痕。

关键约束：
- 旁路异步：由 MQTT 处理器 `create_task` 调用，任何异常都在本模块吞掉，
  绝不影响告警入库与 MQTT 消费；
- 配置热读取：每次派发都从 `notification_config` 读一次，Web 修改后下一条告警立即生效；
- 敏感信息：URL/签名密钥/SMTP 口令只落库，日志、审计、接口均不出现明文。
"""

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiosmtplib
import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionFactory
from app.models import Device, NotificationConfig, NotificationDelivery
from app.schemas import (
    NotificationConfigOut,
    NotificationConfigUpdate,
    NotificationDeliveryOut,
    NotificationTestResult,
)

logger = logging.getLogger(__name__)

CONFIG_ID = 1
MAX_DELIVERY_PAGE = 100


# ==================== 配置读取与写入 ====================


async def get_or_create_config(session: AsyncSession) -> NotificationConfig:
    """读取单行配置；不存在时按安全默认（全部关闭）创建。"""
    config = await session.get(NotificationConfig, CONFIG_ID)
    if config is None:
        config = NotificationConfig(id=CONFIG_ID)
        session.add(config)
        await session.flush()
    return config


def _as_list(value: Optional[str]) -> List[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _as_csv(values: Optional[Sequence[str]]) -> str:
    return ",".join(item.strip() for item in values or [] if item.strip())


def to_out(config: NotificationConfig) -> NotificationConfigOut:
    """对外脱敏：URL、密钥、口令只给布尔「是否已配置」。"""
    return NotificationConfigOut(
        enabled=config.enabled,
        webhook_enabled=config.webhook_enabled,
        webhook_url_configured=bool(config.webhook_url),
        webhook_secret_configured=bool(config.webhook_secret),
        webhook_timeout_seconds=float(config.webhook_timeout_seconds),
        webhook_max_attempts=config.webhook_max_attempts,
        smtp_enabled=config.smtp_enabled,
        smtp_host=config.smtp_host,
        smtp_port=config.smtp_port,
        smtp_username=config.smtp_username,
        smtp_password_configured=bool(config.smtp_password),
        smtp_from=config.smtp_from,
        smtp_to=config.smtp_to,
        smtp_starttls=config.smtp_starttls,
        smtp_timeout_seconds=float(config.smtp_timeout_seconds),
        smtp_max_attempts=config.smtp_max_attempts,
        levels=_as_list(config.levels),
        groups=_as_list(config.groups),
        device_allow=_as_list(config.device_allow),
        device_deny=_as_list(config.device_deny),
        mute_minutes=config.mute_minutes,
        retry_backoff_seconds=float(config.retry_backoff_seconds),
        updated_at=config.updated_at,
        updated_by=config.updated_by,
    )


async def update_config(
    session: AsyncSession, body: NotificationConfigUpdate, operator: str
) -> Tuple[NotificationConfig, List[str]]:
    """按字段更新；返回 (配置, 实际变更的字段名)，字段名用于审计（不含值）。"""
    config = await get_or_create_config(session)
    data = body.model_dump(exclude_unset=True)
    changed: List[str] = []

    scalar_fields = {
        "enabled",
        "webhook_enabled",
        "webhook_timeout_seconds",
        "webhook_max_attempts",
        "smtp_enabled",
        "smtp_host",
        "smtp_port",
        "smtp_username",
        "smtp_from",
        "smtp_to",
        "smtp_starttls",
        "smtp_timeout_seconds",
        "smtp_max_attempts",
        "mute_minutes",
        "retry_backoff_seconds",
    }
    for field in scalar_fields:
        if field in data:
            setattr(config, field, data[field])
            changed.append(field)

    # 敏感字段：None 保持不变，空串清空（接口与审计都不回显值）
    for field in ("webhook_url", "webhook_secret", "smtp_password"):
        if field in data:
            value = data[field]
            if value is not None:
                setattr(config, field, value or None)
                changed.append(field)

    for field in ("levels", "groups", "device_allow", "device_deny"):
        if field in data:
            setattr(config, field, _as_csv(data[field]))
            changed.append(field)

    config.updated_by = operator
    config.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return config, changed


async def list_deliveries(
    session: AsyncSession,
    page: int = 1,
    page_size: int = 20,
    status: Optional[str] = None,
) -> Tuple[List[NotificationDeliveryOut], int]:
    stmt = select(NotificationDelivery)
    count_stmt = select(func.count()).select_from(NotificationDelivery)
    if status:
        stmt = stmt.where(NotificationDelivery.status == status)
        count_stmt = count_stmt.where(NotificationDelivery.status == status)
    total = int((await session.execute(count_stmt)).scalar() or 0)
    rows = (
        (
            await session.execute(
                stmt.order_by(NotificationDelivery.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return [NotificationDeliveryOut.model_validate(row) for row in rows], total


# ==================== 规则与静默 ====================


def matches_rules(config: NotificationConfig, alert: Dict[str, Any], group_name: str) -> bool:
    level = str(alert.get("level") or "")
    if level not in _as_list(config.levels):
        return False
    groups = _as_list(config.groups)
    if groups and group_name not in groups:
        return False
    allow = _as_list(config.device_allow)
    deny = _as_list(config.device_deny)
    device_id = str(alert.get("deviceId") or "")
    if allow and device_id not in allow:
        return False
    if device_id in deny:
        return False
    return True


async def _is_muted(session: AsyncSession, config: NotificationConfig, alert: Dict[str, Any]) -> bool:
    """静默窗口只看最近一次「真正尝试过」的投递，被抑制的记录不延长窗口。"""
    if config.mute_minutes <= 0:
        return False
    since = datetime.now(timezone.utc) - timedelta(minutes=config.mute_minutes)
    latest = (
        await session.execute(
            select(NotificationDelivery.created_at)
            .where(
                NotificationDelivery.device_id == str(alert.get("deviceId") or ""),
                NotificationDelivery.metric == str(alert.get("metric") or ""),
                NotificationDelivery.status.in_(("sent", "failed")),
                NotificationDelivery.created_at >= since,
            )
            .order_by(NotificationDelivery.created_at.desc())
            .limit(1)
        )
    ).scalar()
    return latest is not None


# ==================== 投递 ====================


def _signature(secret: Optional[str], body: bytes) -> Optional[str]:
    if not secret:
        return None
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _safe_error(exc: Exception) -> str:
    """错误信息脱敏：只保留类型与状态码，不带 URL/口令/主机。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "请求超时"
    if isinstance(exc, aiosmtplib.SMTPException):
        return f"SMTP {type(exc).__name__}"
    return f"{type(exc).__name__}"


def _build_payload(alert: Dict[str, Any], device: Optional[Device], test: bool) -> Dict[str, Any]:
    return {
        "event": "notification.test" if test else "alert",
        "sentAt": datetime.now(timezone.utc).isoformat(),
        "alert": alert,
        "device": {
            "deviceId": alert.get("deviceId"),
            "name": alert.get("deviceName") or (device.name if device else ""),
            "groupName": device.group_name if device else "",
            "status": device.status if device else "unknown",
        },
    }


async def _deliver_webhook(
    config: NotificationConfig, alert: Dict[str, Any], device: Optional[Device], test: bool
) -> Dict[str, Any]:
    if not config.webhook_enabled or not config.webhook_url:
        return {"status": "failed", "attempts": 0, "error": "Webhook 未启用或未配置 URL"}

    payload = _build_payload(alert, device, test)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Vikings-Event": payload["event"],
        "X-Vikings-Test": "1" if test else "0",
    }
    signature = _signature(config.webhook_secret, body)
    if signature:
        headers["X-Vikings-Signature"] = signature

    timeout = float(config.webhook_timeout_seconds)
    max_attempts = max(1, int(config.webhook_max_attempts))
    backoff = float(config.retry_backoff_seconds)
    last_error = ""
    attempts = 0

    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    config.webhook_url, content=body, headers=headers
                )
                response.raise_for_status()
            return {"status": "sent", "attempts": attempts, "error": None}
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响主链路
            last_error = _safe_error(exc)
            logger.warning(
                "通知投递失败 channel=webhook device=%s attempt=%d/%d err=%s",
                alert.get("deviceId"), attempts, max_attempts, last_error,
            )
            if attempt < max_attempts and backoff > 0:
                await asyncio.sleep(backoff * (2 ** (attempt - 1)))
    return {"status": "failed", "attempts": attempts, "error": last_error or "投递失败"}


async def _deliver_smtp(
    config: NotificationConfig, alert: Dict[str, Any], device: Optional[Device], test: bool
) -> Dict[str, Any]:
    recipients = _as_list(config.smtp_to)
    if not config.smtp_enabled or not config.smtp_host or not recipients or not config.smtp_from:
        return {"status": "failed", "attempts": 0, "error": "SMTP 未启用或缺少发件人/收件人"}

    level = str(alert.get("level") or "").upper()
    subject = f"[{level}] 设备 {alert.get('deviceId')} {alert.get('message') or '告警'}"
    if test:
        subject = "[测试] " + subject
    body = "\n".join(
        [
            "设备 ID: " + str(alert.get("deviceId")),
            "设备名称: " + str(alert.get("deviceName") or (device.name if device else "")),
            "分组: " + (device.group_name if device else ""),
            "级别: " + level,
            "指标: " + str(alert.get("metric")),
            "当前值: " + str(alert.get("value")),
            "阈值: " + str(alert.get("threshold")),
            "内容: " + str(alert.get("message")),
            "时间: " + datetime.now(timezone.utc).isoformat(),
        ]
    )
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.smtp_from
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    timeout = float(config.smtp_timeout_seconds)
    max_attempts = max(1, int(config.smtp_max_attempts))
    backoff = float(config.retry_backoff_seconds)
    last_error = ""
    attempts = 0

    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            await aiosmtplib.send(
                message,
                hostname=config.smtp_host,
                port=config.smtp_port,
                username=config.smtp_username or None,
                password=config.smtp_password or None,
                start_tls=bool(config.smtp_starttls),
                timeout=timeout,
            )
            return {"status": "sent", "attempts": attempts, "error": None}
        except Exception as exc:  # noqa: BLE001
            last_error = _safe_error(exc)
            logger.warning(
                "通知投递失败 channel=smtp device=%s attempt=%d/%d err=%s",
                alert.get("deviceId"), attempts, max_attempts, last_error,
            )
            if attempt < max_attempts and backoff > 0:
                await asyncio.sleep(backoff * (2 ** (attempt - 1)))
    return {"status": "failed", "attempts": attempts, "error": last_error or "投递失败"}


async def _record(
    session: AsyncSession,
    alert: Dict[str, Any],
    channel: str,
    status: str,
    attempts: int,
    error: Optional[str],
) -> None:
    session.add(
        NotificationDelivery(
            alert_id=str(alert.get("alertId") or ""),
            device_id=str(alert.get("deviceId") or ""),
            metric=str(alert.get("metric") or ""),
            level=str(alert.get("level") or ""),
            channel=channel,
            status=status,
            attempts=attempts,
            error=(error or "")[:255] or None,
        )
    )


async def dispatch_alert(
    alert: Dict[str, Any], session_factory=None
) -> List[Dict[str, Any]]:
    """异步派发一条告警：规则 → 静默 → 各通道投递 → 留痕。

    本函数自行创建会话，调用方必须用 `asyncio.create_task`，不阻塞 MQTT 消费。
    `session_factory` 仅用于测试注入临时库；生产走全局 SessionFactory。
    """
    factory = session_factory or SessionFactory
    try:
        async with factory() as session:
            config = await session.get(NotificationConfig, CONFIG_ID)
            if config is None or not config.enabled:
                return []
            device = await session.get(Device, str(alert.get("deviceId") or ""))
            group_name = device.group_name if device else ""
            if not matches_rules(config, alert, group_name):
                return []

            muted = await _is_muted(session, config, alert)
            if muted:
                await _record(
                    session,
                    alert,
                    channel="mute",
                    status="suppressed",
                    attempts=0,
                    error=f"{config.mute_minutes} 分钟内同设备同指标已通知",
                )
                await session.commit()
                return [{"channel": "mute", "status": "suppressed", "attempts": 0, "error": None}]

            results: List[Dict[str, Any]] = []
            for channel, deliver in (("webhook", _deliver_webhook), ("smtp", _deliver_smtp)):
                if channel == "webhook" and not config.webhook_enabled:
                    continue
                if channel == "smtp" and not config.smtp_enabled:
                    continue
                result = await deliver(config, alert, device, test=False)
                results.append({"channel": channel, **result})
                await _record(
                    session, alert, channel=channel, status=result["status"],
                    attempts=result["attempts"], error=result.get("error"),
                )
            await session.commit()
            return results
    except Exception as exc:  # noqa: BLE001 - 通知故障不得影响告警主链路
        logger.warning("通知派发异常 device=%s err=%s", alert.get("deviceId"), _safe_error(exc))
        return []


async def send_test_notification(
    channel: str, operator: str, session: AsyncSession
) -> List[NotificationTestResult]:
    """界面「测试通知」：真实调用通道，忽略规则/静默，结果逐通道返回。

    复用请求会话，保证测试记录与配置读写在同一个库（也便于测试注入）。
    """
    test_alert = {
        "alertId": "test-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
        "deviceId": "notify-test-device",
        "deviceName": "测试设备",
        "level": "critical",
        "metric": "voltage",
        "value": 2.5,
        "threshold": 2.8,
        "message": "这是一条通道连通性测试通知",
    }
    results: List[NotificationTestResult] = []
    config = await get_or_create_config(session)
    await session.commit()

    for name, deliver in (("webhook", _deliver_webhook), ("smtp", _deliver_smtp)):
        if channel != "all" and channel != name:
            continue
        result = await deliver(config, test_alert, None, test=True)
        results.append(
            NotificationTestResult(
                channel=name,
                status=result["status"],
                attempts=result["attempts"],
                error=result.get("error"),
            )
        )
        await _record(
            session, test_alert, channel=name, status=result["status"],
            attempts=result["attempts"], error=result.get("error"),
        )
        await session.commit()
        logger.info(
            "测试通知 operator=%s channel=%s status=%s attempts=%d",
            operator, name, result["status"], result["attempts"],
        )
    return results
