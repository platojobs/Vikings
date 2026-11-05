"""告警通知（v1.31.0）：多规则路由 + 持久化重试队列 + Webhook/SMTP 投递。

关键约束（延续 v1.29.0）：
- 旁路异步：由 MQTT 处理器 `create_task` 调用，任何异常都在本模块吞掉，
  绝不影响告警入库与 MQTT 消费；
- 配置热读取：每次派发读库，Web 修改后下一条告警立即生效；
- 敏感信息：URL/签名密钥/SMTP 口令只落库，日志、审计、接口均不出现明文。

v1.31.0 新增：
- 规则表 `notification_rule`：一条告警按 `priority ASC, id ASC` 命中**第一条**
  可用规则（有渠道且条件匹配），不再使用单行配置里的规则字段；
- 投递先落库再发送：`notification_delivery.status=pending` + `next_attempt_at`
  即持久化重试队列，网关重启后调度器续投；
- 投递记录扩充规则/分组快照、累计耗时与下次重试时间，供独立历史页筛选。
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
import weakref
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiosmtplib
import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionFactory
from app.models import (
    Device,
    DeviceAlert,
    NotificationConfig,
    NotificationDelivery,
    NotificationRule,
)
from app.schemas import (
    NotificationConfigOut,
    NotificationConfigUpdate,
    NotificationDeliveryOut,
    NotificationDeliveryPageOut,
    NotificationRuleCreate,
    NotificationRuleOut,
    NotificationRuleUpdate,
    NotificationTestResult,
)

logger = logging.getLogger(__name__)

CONFIG_ID = 1
MAX_DELIVERY_PAGE = 100
RETRY_BATCH = 20
# 同时进行的告警派发上限：告警风暴时通知任务不与 MQTT 上报争抢事件循环/数据库连接。
# 等待中的任务只是挂起协程，不占用连接，也不会阻塞上报处理。
DISPATCH_CONCURRENCY = 3
_DISPATCH_GATES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _as_utc(value: datetime) -> datetime:
    """投递历史的时间筛选统一按 UTC 比较；无时区入参视为 UTC。"""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _dispatch_gate() -> asyncio.Semaphore:
    """按事件循环取派发闸门；测试会重复创建 loop，不能共用一个 Semaphore。"""
    loop = asyncio.get_running_loop()
    gate = _DISPATCH_GATES.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(DISPATCH_CONCURRENCY)
        _DISPATCH_GATES[loop] = gate
    return gate


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


# ==================== 规则 CRUD ====================


def rule_to_out(rule: NotificationRule) -> NotificationRuleOut:
    return NotificationRuleOut(
        id=rule.id,
        name=rule.name,
        enabled=rule.enabled,
        priority=rule.priority,
        levels=_as_list(rule.levels),
        groups=_as_list(rule.groups),
        device_allow=_as_list(rule.device_allow),
        device_deny=_as_list(rule.device_deny),
        webhook_enabled=rule.webhook_enabled,
        webhook_url_configured=bool(rule.webhook_url),
        webhook_secret_configured=bool(rule.webhook_secret),
        smtp_enabled=rule.smtp_enabled,
        smtp_to=rule.smtp_to,
        updated_at=rule.updated_at,
        updated_by=rule.updated_by,
    )


async def list_rules(session: AsyncSession) -> List[NotificationRule]:
    """按优先级排序返回全部规则（含停用），供界面展示与裁决顺序核对。"""
    rows = (
        (
            await session.execute(
                select(NotificationRule).order_by(NotificationRule.priority, NotificationRule.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def _validate_rule(rule: NotificationRule) -> None:
    """规则必须至少有一个可用渠道，避免保存后永远投不出去。"""
    if rule.webhook_enabled and not rule.webhook_url:
        raise ValueError("启用 Webhook 时必须配置 URL")
    if rule.smtp_enabled and not _as_list(rule.smtp_to):
        raise ValueError("启用 SMTP 时必须填写收件人")
    if not rule.webhook_enabled and not rule.smtp_enabled:
        raise ValueError("规则至少需要启用一个通道")


async def create_rule(
    session: AsyncSession, body: NotificationRuleCreate, operator: str
) -> NotificationRule:
    rule = NotificationRule(
        name=body.name.strip(),
        enabled=body.enabled,
        priority=body.priority,
        levels=_as_csv(body.levels),
        groups=_as_csv(body.groups),
        device_allow=_as_csv(body.device_allow),
        device_deny=_as_csv(body.device_deny),
        webhook_enabled=body.webhook_enabled,
        webhook_url=body.webhook_url or None,
        webhook_secret=body.webhook_secret or None,
        smtp_enabled=body.smtp_enabled,
        smtp_to=body.smtp_to or None,
        updated_by=operator,
        updated_at=datetime.now(timezone.utc),
    )
    _validate_rule(rule)
    session.add(rule)
    await session.flush()
    return rule


async def update_rule(
    session: AsyncSession, rule_id: int, body: NotificationRuleUpdate, operator: str
) -> Tuple[Optional[NotificationRule], List[str]]:
    """更新规则；返回 (规则, 变更字段名)。URL/密钥/口令不返回值本身。"""
    rule = await session.get(NotificationRule, rule_id)
    if rule is None:
        return None, []
    data = body.model_dump(exclude_unset=True)
    changed: List[str] = []

    for field in ("name", "enabled", "priority", "webhook_enabled", "smtp_enabled"):
        if field in data and data[field] is not None:
            setattr(rule, field, data[field].strip() if field == "name" else data[field])
            changed.append(field)

    # 敏感字段：None 保持不变，空串清空
    for field in ("webhook_url", "webhook_secret"):
        if field in data:
            value = data[field]
            if value is not None:
                setattr(rule, field, value or None)
                changed.append(field)

    if "smtp_to" in data and data["smtp_to"] is not None:
        rule.smtp_to = data["smtp_to"] or None
        changed.append("smtp_to")

    for field in ("levels", "groups", "device_allow", "device_deny"):
        if field in data and data[field] is not None:
            setattr(rule, field, _as_csv(data[field]))
            changed.append(field)

    _validate_rule(rule)
    rule.updated_by = operator
    rule.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return rule, changed


async def delete_rule(session: AsyncSession, rule_id: int) -> Optional[NotificationRule]:
    rule = await session.get(NotificationRule, rule_id)
    if rule is None:
        return None
    await session.delete(rule)
    await session.flush()
    return rule


# ==================== 风格兼容：规则条件判定 ====================


def matches_rules(rule: Any, alert: Dict[str, Any], group_name: str) -> bool:
    """级别 + 分组 + 设备白/黑名单组合判定（deny 优先），三个维度是且关系。"""
    level = str(alert.get("level") or "")
    if level not in _as_list(rule.levels):
        return False
    groups = _as_list(rule.groups)
    if groups and group_name not in groups:
        return False
    allow = _as_list(rule.device_allow)
    deny = _as_list(rule.device_deny)
    device_id = str(alert.get("deviceId") or "")
    if allow and device_id not in allow:
        return False
    if device_id in deny:
        return False
    return True


def _rule_usable(rule: NotificationRule) -> bool:
    return (rule.webhook_enabled and bool(rule.webhook_url)) or (
        rule.smtp_enabled and bool(_as_list(rule.smtp_to))
    )


class _LegacyRule:
    """v1.29.0 单行配置的只读适配层。

    仅当 `notification_rule` 表完全没有数据（例如从旧版本升级但配置是在迁移之后
    才写入的）时启用，保证升级后行为不丢；一旦管理端创建了任何规则即不再回退。
    """

    id = None
    priority = 100
    enabled = True

    def __init__(self, config: NotificationConfig) -> None:
        self.name = "默认规则（兼容 v1.29.0 配置）"
        self.levels = config.levels
        self.groups = config.groups
        self.device_allow = config.device_allow
        self.device_deny = config.device_deny
        self.webhook_enabled = config.webhook_enabled
        self.webhook_url = config.webhook_url
        self.webhook_secret = config.webhook_secret
        self.smtp_enabled = config.smtp_enabled
        self.smtp_to = config.smtp_to
        self.updated_at = getattr(config, "updated_at", None)
        self.updated_by = getattr(config, "updated_by", "system")


async def resolve_rule(
    session: AsyncSession, config: NotificationConfig, alert: Dict[str, Any], group_name: str
) -> Optional[Any]:
    """按优先级裁决：priority 升序、同优先级 id 升序，取第一条可用且命中的规则。"""
    rules = await list_rules(session)
    if not rules:
        legacy = _LegacyRule(config)
        if _rule_usable(legacy) and matches_rules(legacy, alert, group_name):
            return legacy
        return None
    for rule in rules:
        if rule.enabled and _rule_usable(rule) and matches_rules(rule, alert, group_name):
            return rule
    return None


# ==================== 投递实现 ====================


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


def _build_payload(
    alert: Dict[str, Any],
    device: Optional[Device],
    test: bool,
    delivery_id: Optional[int] = None,
    rule: Optional[Any] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """通知载荷：v1.33.0 起支持升级/交接的附加块。

    `extra` 由投递记录持久化，网关重启后重试仍能还原「第几次升级 / 原 SLA」
    或「交接人 / 接收人 / 条数 / 时间窗口」，不依赖进程内内存。
    """
    payload = {
        "event": "notification.test" if test else (extra or {}).get("event", "alert"),
        "sentAt": datetime.now(timezone.utc).isoformat(),
        # 接收端幂等键：同一 delivery 在崩溃/重试后可能重复到达，按此去重
        "deliveryId": delivery_id,
        "rule": {"id": rule.id, "name": rule.name} if rule is not None else None,
        "alert": alert,
        "device": {
            "deviceId": alert.get("deviceId"),
            "name": alert.get("deviceName") or (device.name if device else ""),
            "groupName": device.group_name if device else "",
            "status": device.status if device else "unknown",
        },
    }
    if extra:
        for key in ("escalation", "handover"):
            if key in extra:
                payload[key] = extra[key]
    return payload


async def _attempt_webhook(
    rule: Any,
    alert: Dict[str, Any],
    device: Optional[Device],
    test: bool,
    timeout: float,
    delivery_id: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[str], int]:
    """单次 Webhook 尝试；返回 (是否成功, 脱敏错误, 耗时毫秒)。"""
    payload = _build_payload(alert, device, test, delivery_id, rule, extra)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Vikings-Event": payload["event"],
        "X-Vikings-Test": "1" if test else "0",
    }
    if delivery_id is not None:
        headers["X-Vikings-Delivery-Id"] = str(delivery_id)
        headers["X-Vikings-Rule-Id"] = str(rule.id)
    signature = _signature(rule.webhook_secret, body)
    if signature:
        headers["X-Vikings-Signature"] = signature

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(rule.webhook_url, content=body, headers=headers)
            response.raise_for_status()
        return True, None, int((time.perf_counter() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 - 通知失败不影响主链路
        return False, _safe_error(exc), int((time.perf_counter() - started) * 1000)


async def _attempt_smtp(
    config: NotificationConfig,
    rule: Any,
    alert: Dict[str, Any],
    device: Optional[Device],
    test: bool,
    timeout: float,
    delivery_id: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[str], int]:
    """单次 SMTP 尝试；SMTP 服务器沿用全局配置，收件人来自规则。"""
    recipients = _as_list(rule.smtp_to)

    level = str(alert.get("level") or "").upper()
    subject = f"[{level}] 设备 {alert.get('deviceId')} {alert.get('message') or '告警'}"
    if test:
        subject = "[测试] " + subject
    extra = extra or {}
    escalation = extra.get("escalation")
    handover = extra.get("handover")
    if escalation:
        subject = (
            f"[超时升级 第{escalation.get('attempt')}/{escalation.get('maxAttempts')}次] "
            f"设备 {alert.get('deviceId')} {alert.get('message') or '告警'}"
        )
    elif handover:
        subject = f"[值班交接] {handover.get('fromUser')} → {handover.get('to')}（{handover.get('count')} 条）"

    lines = [
        "设备 ID: " + str(alert.get("deviceId")),
        "设备名称: " + str(alert.get("deviceName") or (device.name if device else "")),
        "分组: " + (device.group_name if device else ""),
        "级别: " + level,
        "指标: " + str(alert.get("metric")),
        "当前值: " + str(alert.get("value")),
        "阈值: " + str(alert.get("threshold")),
        "内容: " + str(alert.get("message")),
        "规则: " + str(rule.name),
        "时间: " + datetime.now(timezone.utc).isoformat(),
    ]
    if escalation:
        lines.extend(
            [
                "升级类型: 超时升级",
                f"第几次: {escalation.get('attempt')} / {escalation.get('maxAttempts')}",
                f"原 SLA: {escalation.get('slaMinutes')} 分钟",
                f"已超出: {escalation.get('overdueMinutes')} 分钟",
            ]
        )
    if handover:
        lines.extend(
            [
                "交接类型: 值班交接汇总",
                f"交接人: {handover.get('fromUser')}",
                f"接收人: {handover.get('to')}",
                f"交接条数: {handover.get('count')}",
                f"告警时间窗口: {handover.get('windowStart')} ~ {handover.get('windowEnd')}",
                f"操作人: {handover.get('operator')}",
            ]
        )
    body = "\n".join(lines)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.smtp_from or ""
    message["To"] = ", ".join(recipients)
    message.set_content(body)
    # 确定性 Message-ID：重试/重启续投后接收端可按同一 ID 去重
    if delivery_id is not None:
        message["Message-ID"] = f"<vikings-delivery-{delivery_id}@vikings.local>"

    started = time.perf_counter()
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
        return True, None, int((time.perf_counter() - started) * 1000)
    except Exception as exc:  # noqa: BLE001
        return False, _safe_error(exc), int((time.perf_counter() - started) * 1000)


def _channel_max_attempts(config: NotificationConfig, channel: str) -> int:
    value = config.webhook_max_attempts if channel == "webhook" else config.smtp_max_attempts
    return max(1, int(value))


def _channel_timeout(config: NotificationConfig, channel: str) -> float:
    value = config.webhook_timeout_seconds if channel == "webhook" else config.smtp_timeout_seconds
    return float(value)


def _channel_missing_reason(config: NotificationConfig, rule: Any, channel: str) -> Optional[str]:
    if channel == "webhook":
        if not rule.webhook_enabled:
            return "规则未启用 Webhook"
        if not rule.webhook_url:
            return "规则未配置 Webhook URL"
        return None
    if not rule.smtp_enabled:
        return "规则未启用 SMTP"
    if not _as_list(rule.smtp_to):
        return "规则未配置收件人"
    if not config.smtp_host or not config.smtp_from:
        return "全局 SMTP 服务器或发件人未配置"
    return None


# ==================== 持久化队列 ====================


async def _is_muted(session: AsyncSession, config: NotificationConfig, alert: Dict[str, Any]) -> bool:
    """静默窗口只看最近一次「真正尝试过」的投递，被抑制/待重试的记录不延长窗口。"""
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


def _new_delivery(
    alert: Dict[str, Any],
    channel: str,
    rule: Optional[Any],
    group_name: str,
    *,
    status: str = "pending",
    error: Optional[str] = None,
    kind: str = "alert",
    extra: Optional[Dict[str, Any]] = None,
) -> NotificationDelivery:
    now = datetime.now(timezone.utc)
    return NotificationDelivery(
        alert_id=str(alert.get("alertId") or ""),
        device_id=str(alert.get("deviceId") or ""),
        metric=str(alert.get("metric") or ""),
        level=str(alert.get("level") or "")[:16],
        channel=channel,
        status=status,
        kind=kind,
        extra=extra,
        attempts=0,
        error=(error or "")[:255] or None,
        rule_id=getattr(rule, "id", None) if rule is not None else None,
        rule_name=(str(getattr(rule, "name", "") or "")[:64] or None) if rule is not None else None,
        group_name=(group_name or "")[:64] or None,
        duration_ms=0,
        next_attempt_at=now if status == "pending" else None,
        finished_at=now if status in ("sent", "failed", "suppressed") else None,
    )


async def _claim(session: AsyncSession, delivery_id: int) -> bool:
    """原子领取一条待投递记录：先租约后尝试，避免调度器与首投重复发送。"""
    now = datetime.now(timezone.utc)
    result = await session.execute(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.id == delivery_id,
            NotificationDelivery.status == "pending",
            NotificationDelivery.next_attempt_at <= now,
        )
        .values(next_attempt_at=now + timedelta(seconds=60), updated_at=now)
    )
    await session.commit()
    return int(result.rowcount or 0) > 0


def _summary(row: NotificationDelivery) -> Dict[str, Any]:
    return {
        "deliveryId": row.id,
        "channel": row.channel,
        "status": row.status,
        "attempts": row.attempts,
        "error": row.error,
    }


async def process_delivery(
    delivery_id: int,
    session_factory=None,
    *,
    context: Optional[Tuple[NotificationConfig, Any, Optional[Device], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """执行一次投递尝试（首投与重试共用），失败时按退避排下一次或落终态。

    `context` 由刚创建队列行的 dispatch_alert 传入（配置/规则/设备/告警已在内存），
    省掉首投时重复的 4 次查询，避免通知任务和上报处理抢数据库连接。
    """
    factory = session_factory or SessionFactory

    # ---- 阶段 1：领取 + 读取投递所需数据（只占用连接做查询，不横跨网络请求）----
    async with factory() as session:
        claimed = await _claim(session, delivery_id)
        row = await session.get(NotificationDelivery, delivery_id)
        if row is None:
            return {"deliveryId": delivery_id, "channel": "", "status": "failed", "attempts": 0, "error": "投递记录不存在"}
        if not claimed or row.status != "pending":
            # 已被其他协程领取或已终态：直接返回现状，不重复发送
            return _summary(row)

        channel = row.channel
        # 升级/交接的附加载荷从投递记录读取：首投与重启后重试走同一份快照
        extra = dict(row.extra or {})
        if context is not None:
            config, rule, device, alert = context
        else:
            config = await session.get(NotificationConfig, CONFIG_ID)
            if config is None or not config.enabled:
                row.status = "failed"
                row.error = "通知总开关已关闭"
                row.next_attempt_at = None
                row.finished_at = datetime.now(timezone.utc)
                await session.commit()
                return _summary(row)

            if row.rule_id is not None:
                rule = await session.get(NotificationRule, row.rule_id)
                if rule is None:
                    row.status = "failed"
                    row.error = "规则已删除，无法继续投递"
                    row.next_attempt_at = None
                    row.finished_at = datetime.now(timezone.utc)
                    await session.commit()
                    return _summary(row)
            else:
                rule = _LegacyRule(config)

            device = await session.get(Device, row.device_id)
            if row.kind == "handover":
                # 交接汇总不对应具体告警，载荷全部来自投递记录里的快照
                handover = extra.get("handover", {})
                alert = {
                    "alertId": row.alert_id,
                    "deviceId": row.device_id,
                    "deviceName": f"值班交接 → {handover.get('to', '')}",
                    "level": row.level,
                    "metric": "handover",
                    "value": handover.get("count"),
                    "threshold": 0,
                    "message": handover.get("message") or "值班交接汇总",
                    "aiGenerated": False,
                }
            else:
                # 重试可能发生在网关重启后，内存里的原始告警已丢失：从告警表重建内容
                alert_row = (
                    await session.execute(
                        select(DeviceAlert).where(DeviceAlert.alert_id == row.alert_id).limit(1)
                    )
                ).scalar_one_or_none()
                alert = {
                    "alertId": row.alert_id,
                    "deviceId": row.device_id,
                    "deviceName": device.name if device else row.device_id,
                    "level": row.level,
                    "metric": row.metric,
                    "value": float(alert_row.value) if alert_row else None,
                    "threshold": float(alert_row.threshold) if alert_row else None,
                    "message": alert_row.message if alert_row else f"{row.metric} 越过阈值",
                    "aiGenerated": bool(alert_row.ai_generated) if alert_row else False,
                }

        missing = _channel_missing_reason(config, rule, channel)
        if missing:
            row.status = "failed"
            row.error = missing
            row.next_attempt_at = None
            row.finished_at = datetime.now(timezone.utc)
            await session.commit()
            return _summary(row)

        timeout = _channel_timeout(config, channel)

    # ---- 阶段 2：执行网络尝试（不持有数据库连接，慢接收端不会拖垮连接池）----
    if channel == "webhook":
        ok, error, elapsed_ms = await _attempt_webhook(
            rule, alert, device, test=False, timeout=timeout, delivery_id=delivery_id, extra=extra
        )
    else:
        ok, error, elapsed_ms = await _attempt_smtp(
            config, rule, alert, device, test=False, timeout=timeout, delivery_id=delivery_id,
            extra=extra,
        )

    # ---- 阶段 3：写回尝试结果（单次短事务）----
    async with factory() as session:
        row = await session.get(NotificationDelivery, delivery_id)
        if row is None:
            return {"deliveryId": delivery_id, "channel": channel, "status": "failed", "attempts": 0, "error": "投递记录不存在"}
        if row.status != "pending":
            # 租约过期后被其他实例改过状态：以库中状态为准，不重复记账
            return _summary(row)

        row.attempts += 1
        row.duration_ms += elapsed_ms
        row.updated_at = datetime.now(timezone.utc)
        if ok:
            row.status = "sent"
            row.error = None
            row.next_attempt_at = None
            row.finished_at = row.updated_at
        else:
            row.error = error or "投递失败"
            logger.warning(
                "通知投递失败 channel=%s delivery=%s device=%s attempt=%d/%d err=%s",
                row.channel, row.id, row.device_id, row.attempts,
                _channel_max_attempts(config, row.channel), row.error,
            )
            if row.attempts >= _channel_max_attempts(config, row.channel):
                row.status = "failed"
                row.next_attempt_at = None
                row.finished_at = row.updated_at
            else:
                backoff = float(config.retry_backoff_seconds)
                row.next_attempt_at = row.updated_at + timedelta(
                    seconds=backoff * (2 ** (row.attempts - 1))
                )
        await session.commit()
        if ok and row.attempts > 1:
            # 续投成功（可能发生在网关重启之后）：留一条日志，值班交接时可核对
            logger.info(
                "通知重试成功 channel=%s delivery=%s device=%s attempt=%d duration_ms=%d",
                row.channel, row.id, row.device_id, row.attempts, row.duration_ms,
            )
        return _summary(row)


async def retry_due_deliveries(session_factory=None) -> int:
    """调度器入口：把到期的 pending 记录逐条续投（单进程内顺序执行）。"""
    factory = session_factory or SessionFactory
    async with factory() as session:
        ids = list(
            (
                await session.execute(
                    select(NotificationDelivery.id)
                    .where(
                        NotificationDelivery.status == "pending",
                        NotificationDelivery.next_attempt_at <= datetime.now(timezone.utc),
                    )
                    .order_by(NotificationDelivery.next_attempt_at)
                    .limit(RETRY_BATCH)
                )
            ).scalars()
        )
    processed = 0
    for delivery_id in ids:
        try:
            await process_delivery(int(delivery_id), factory)
            processed += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响后续重试
            logger.warning(
                "重试队列处理失败 delivery=%s err=%s", delivery_id, _safe_error(exc)
            )
    return processed


async def list_deliveries(
    session: AsyncSession,
    page: int = 1,
    page_size: int = 20,
    status: Optional[str] = None,
    channel: Optional[str] = None,
    device_id: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> NotificationDeliveryPageOut:
    """投递历史：服务端统一筛选与分页，Web 只做参数透传。"""
    stmt = select(NotificationDelivery)
    count_stmt = select(func.count()).select_from(NotificationDelivery)
    conditions = []
    if status:
        conditions.append(NotificationDelivery.status == status)
    if channel:
        conditions.append(NotificationDelivery.channel == channel)
    if device_id:
        conditions.append(NotificationDelivery.device_id == device_id)
    if start:
        conditions.append(NotificationDelivery.created_at >= _as_utc(start))
    if end:
        conditions.append(NotificationDelivery.created_at <= _as_utc(end))
    for condition in conditions:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    total = int((await session.execute(count_stmt)).scalar() or 0)
    rows = (
        (
            await session.execute(
                stmt.order_by(NotificationDelivery.created_at.desc(), NotificationDelivery.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return NotificationDeliveryPageOut(
        items=[NotificationDeliveryOut.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


async def _resolve_handover_rule(
    session: AsyncSession, config: NotificationConfig, level: str
) -> Optional[Any]:
    """交接汇总的规则裁决：按优先级取第一条可用规则。

    与普通告警不同，交接是多设备汇总、没有单一分组，因此**忽略规则里的分组/设备
    条件**，只按级别与可用渠道筛选；否则「交接通知」会因分组不匹配而永远发不出。
    其余行为（priority ASC, id ASC、无规则时回退 v1.29.0 单行配置）与告警一致。
    """
    rules = await list_rules(session)
    if not rules:
        legacy = _LegacyRule(config)
        if _rule_usable(legacy) and (not _as_list(legacy.levels) or level in _as_list(legacy.levels)):
            return legacy
        return None
    for rule in rules:
        if not rule.enabled or not _rule_usable(rule):
            continue
        levels = _as_list(rule.levels)
        if levels and level not in levels:
            continue
        return rule
    return None


async def send_handover_summary(
    *,
    to: str,
    from_user: str,
    count: int,
    window_start: Optional[datetime],
    window_end: Optional[datetime],
    level: str,
    operator: str,
    note: Optional[str] = None,
    session_factory=None,
) -> List[Dict[str, Any]]:
    """值班交接汇总：复用 v1.31.0 的规则链路与投递队列，只发**一条汇总**。

    设计取舍：
    - 不是给每条告警发一次通知（N 条会淹没接收人），而是把条数与时间窗口聚合成
      一条 `alert.handover` 事件；
    - 仍先落 `notification_delivery` 再首投，网关重启后由调度器续投；
    - 交接汇总没有具体设备，按级别选第一条可用规则，忽略分组/设备过滤条件。
    """
    async with _dispatch_gate():
        factory = session_factory or SessionFactory
        alert = {
            "alertId": "hnd-" + uuid.uuid4().hex[:12],
            "deviceId": "handover",
            "deviceName": f"值班交接 → {to}",
            "level": level,
            "metric": "handover",
            "value": count,
            "threshold": 0,
            "message": f"值班交接：{from_user} → {to}，共 {count} 条未处理告警",
            "aiGenerated": False,
        }
        extra = {
            "event": "alert.handover",
            "handover": {
                "fromUser": from_user,
                "to": to,
                "count": count,
                "windowStart": window_start.isoformat() if window_start else None,
                "windowEnd": window_end.isoformat() if window_end else None,
                "operator": operator,
                "note": note,
                "message": alert["message"],
            },
        }

        ids: List[int] = []
        async with factory() as session:
            config = await session.get(NotificationConfig, CONFIG_ID)
            if config is None or not config.enabled:
                logger.info("交接汇总未发送：通知总开关关闭 to=%s count=%d", to, count)
                return []
            rule = await _resolve_handover_rule(session, config, level)
            if rule is None:
                row = _new_delivery(
                    alert, channel="rule", rule=None, group_name="",
                    status="suppressed", error="未匹配任何可用通知规则", kind="handover", extra=extra,
                )
                session.add(row)
                await session.commit()
                return [{"channel": "rule", "status": "suppressed", "attempts": 0, "error": row.error}]

            for channel in ("webhook", "smtp"):
                if _channel_missing_reason(config, rule, channel) is None:
                    row = _new_delivery(
                        alert, channel=channel, rule=rule, group_name="",
                        status="pending", kind="handover", extra=extra,
                    )
                    session.add(row)
                    await session.flush()
                    ids.append(row.id)
            if not ids:
                row = _new_delivery(
                    alert, channel="rule", rule=rule, group_name="",
                    status="suppressed", error="规则未启用任何可用渠道（Webhook/SMTP）",
                    kind="handover", extra=extra,
                )
                session.add(row)
                await session.commit()
                return [{"channel": "rule", "status": "suppressed", "attempts": 0, "error": row.error}]
            await session.commit()

        results = []
        for delivery_id in ids:
            results.append(await process_delivery(delivery_id, factory))
        return results


# ==================== 派发入口 ====================


async def dispatch_alert(
    alert: Dict[str, Any], session_factory=None
) -> List[Dict[str, Any]]:
    """异步派发一条告警：规则裁决 → 静默 → **先落库** → 首投 → 留痕。

    本函数自行创建会话，调用方必须用 `asyncio.create_task`，不阻塞 MQTT 消费。
    首投结果可能是 `pending`（已排队，由调度器续投），也可能是终态。

    派发并发由 `_dispatch_gate()` 限制（默认 3）：等待中的任务只是挂起协程，
    不占数据库连接，告警风暴时也不会拖慢上报处理；网关重启后由调度器续投。
    """
    async with _dispatch_gate():
        return await _dispatch_alert_impl(alert, session_factory=session_factory)


async def _dispatch_alert_impl(
    alert: Dict[str, Any], session_factory=None
) -> List[Dict[str, Any]]:
    factory = session_factory or SessionFactory
    queued: List[int] = []
    try:
        async with factory() as session:
            config = await session.get(NotificationConfig, CONFIG_ID)
            if config is None or not config.enabled:
                return []
            device = await session.get(Device, str(alert.get("deviceId") or ""))
            group_name = device.group_name if device else ""
            rule = await resolve_rule(session, config, alert, group_name)

            if rule is None:
                row = _new_delivery(
                    alert,
                    channel="rule",
                    rule=None,
                    group_name=group_name,
                    status="suppressed",
                    error=f"未匹配任何通知规则（设备分组: {group_name or '未知'}）",
                )
                session.add(row)
                await session.commit()
                return [{"channel": "rule", "status": "suppressed", "attempts": 0, "error": row.error}]

            muted = await _is_muted(session, config, alert)
            if muted:
                row = _new_delivery(
                    alert,
                    channel="mute",
                    rule=rule,
                    group_name=group_name,
                    status="suppressed",
                    error=f"{config.mute_minutes} 分钟内同设备同指标已通知",
                )
                session.add(row)
                await session.commit()
                return [{"channel": "mute", "status": "suppressed", "attempts": 0, "error": None}]

            for channel in ("webhook", "smtp"):
                if _channel_missing_reason(config, rule, channel):
                    continue
                row = _new_delivery(alert, channel=channel, rule=rule, group_name=group_name)
                session.add(row)
                await session.flush()
                queued.append(row.id)

            if not queued:
                row = _new_delivery(
                    alert,
                    channel="rule",
                    rule=rule,
                    group_name=group_name,
                    status="suppressed",
                    error=f"规则「{rule.name}」没有可用接收人（检查 URL/收件人/全局 SMTP）",
                )
                session.add(row)
                await session.commit()
                return [{"channel": "rule", "status": "suppressed", "attempts": 0, "error": row.error}]
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - 通知故障不得影响告警主链路
        logger.warning("通知派发异常 device=%s err=%s", alert.get("deviceId"), _safe_error(exc))
        return []

    results: List[Dict[str, Any]] = []
    context = (config, rule, device, alert)
    for delivery_id in queued:
        try:
            results.append(await process_delivery(delivery_id, factory, context=context))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "首投异常 delivery=%s err=%s", delivery_id, _safe_error(exc)
            )
    return results


# ==================== 测试通知 ====================


async def _pick_test_rule(
    session: AsyncSession, config: NotificationConfig, channel: str, rule_id: Optional[int]
) -> Optional[Any]:
    if rule_id is not None:
        rule = await session.get(NotificationRule, rule_id)
        return rule
    for rule in await list_rules(session):
        if not rule.enabled:
            continue
        if channel == "webhook" and rule.webhook_enabled and rule.webhook_url:
            return rule
        if channel == "smtp" and rule.smtp_enabled and _as_list(rule.smtp_to):
            return rule
    if not await list_rules(session):
        return _LegacyRule(config)
    return None


async def send_test_notification(
    channel: str, operator: str, session: AsyncSession, rule_id: Optional[int] = None
) -> List[NotificationTestResult]:
    """界面「测试通知」：真实调用通道，忽略规则匹配与静默，结果逐通道返回。

    测试不进入重试队列：一次结果即可证明连通性，失败原因直接回到界面。
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

    for name in ("webhook", "smtp"):
        if channel != "all" and channel != name:
            continue
        rule = await _pick_test_rule(session, config, name, rule_id)
        if rule is None:
            results.append(
                NotificationTestResult(
                    channel=name, status="failed", attempts=0,
                    error=f"没有可用于 {name} 的通知规则",
                )
            )
            continue
        missing = _channel_missing_reason(config, rule, name)
        if missing:
            results.append(
                NotificationTestResult(channel=name, status="failed", attempts=0, error=missing)
            )
            continue

        timeout = _channel_timeout(config, name)
        max_attempts = _channel_max_attempts(config, name)
        backoff = float(config.retry_backoff_seconds)
        attempts = 0
        last_error: Optional[str] = None
        total_ms = 0
        ok = False
        while attempts < max_attempts:
            attempts += 1
            if name == "webhook":
                ok, last_error, elapsed_ms = await _attempt_webhook(
                    rule, test_alert, None, test=True, timeout=timeout
                )
            else:
                ok, last_error, elapsed_ms = await _attempt_smtp(
                    config, rule, test_alert, None, test=True, timeout=timeout
                )
            total_ms += elapsed_ms
            if ok:
                break
            if attempts < max_attempts and backoff > 0:
                await asyncio.sleep(backoff * (2 ** (attempts - 1)))

        status = "sent" if ok else "failed"
        error_text = None if ok else ((last_error or "投递失败")[:255] or "投递失败")
        results.append(
            NotificationTestResult(
                channel=name, status=status, attempts=attempts,
                error=error_text,
            )
        )
        session.add(
            NotificationDelivery(
                alert_id=test_alert["alertId"],
                device_id=test_alert["deviceId"],
                metric=test_alert["metric"],
                level=test_alert["level"],
                channel=name,
                status=status,
                attempts=attempts,
                error=error_text,
                rule_id=getattr(rule, "id", None),
                rule_name=(str(getattr(rule, "name", "") or "")[:64] or None),
                group_name=None,
                duration_ms=total_ms,
                next_attempt_at=None,
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        logger.info(
            "测试通知 operator=%s channel=%s status=%s attempts=%d rule=%s",
            operator, name, status, attempts, getattr(rule, "id", None),
        )
    return results
