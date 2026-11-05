"""告警 SLA 与值班交接（v1.33.0）。

口径（统一在服务端计算，Web / 移动端只消费 `sla_state`，不各自算一套）：

- 响应时限：每条告警在创建时按级别快照 `sla_minutes` 与 `sla_deadline_at`，
  之后修改 SLA 配置只影响新告警，历史告警口径稳定、可复盘；
- 已确认（status=acked 且 acked_at 有值）：
  * `met`   达标：acked_at <= deadline
  * `late`  超时达成：acked_at > deadline
  * `unknown` 历史确认但无 acked_at（v1.21 之前的旧行）：**不计达标**，
    单独计数，避免用默认值把「没有处置证据」洗成达标；
- 未确认：
  * `pending` 未处理未超时（剩余秒数为正）
  * `overdue` 未处理已超时（剩余秒数为负，绝对值为超出时长）

超时升级：
- 触发时间 = deadline + escalation_count * escalation_interval_minutes；
- 每次升级用「条件自增」原子领取名额（update ... where escalation_count = 旧值），
  因此同一告警同一级别只会发一次，网关重启/多实例巡检也不会重复；
- 达到 `max_escalations` 后停止升级，但告警继续保持 `overdue`（升级停止 ≠ 告警消失）；
- 通知内容复用 v1.31.0 规则链路与持久化投递队列，`extra` 里固化「第几次/上限/原 SLA」。
"""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import SessionFactory
from app.models import Device, DeviceAlert, SlaConfig
from app.schemas import AlertOut, SlaConfigOut, SlaConfigUpdate
from app.services import notify_service

logger = logging.getLogger(__name__)
settings = get_settings()

SLA_CONFIG_ID = 1

STATE_MET = "met"
STATE_LATE = "late"
STATE_PENDING = "pending"
STATE_OVERDUE = "overdue"
STATE_UNKNOWN = "unknown"
SLA_STATES = (STATE_MET, STATE_LATE, STATE_PENDING, STATE_OVERDUE, STATE_UNKNOWN)

SWEEP_BATCH = 200


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


# ==================== SLA 配置 ====================


async def get_or_create_config(session: AsyncSession) -> SlaConfig:
    config = await session.get(SlaConfig, SLA_CONFIG_ID)
    if config is None:
        config = SlaConfig(id=SLA_CONFIG_ID)
        session.add(config)
        await session.flush()
    return config


def to_out(config: SlaConfig) -> SlaConfigOut:
    return SlaConfigOut(
        critical_minutes=config.critical_minutes,
        warning_minutes=config.warning_minutes,
        max_escalations=config.max_escalations,
        escalation_interval_minutes=config.escalation_interval_minutes,
        updated_at=config.updated_at,
        updated_by=config.updated_by,
    )


async def update_config(
    session: AsyncSession, body: SlaConfigUpdate, operator: str
) -> Tuple[SlaConfig, List[str]]:
    config = await get_or_create_config(session)
    data = body.model_dump(exclude_unset=True)
    changed: List[str] = []
    for field, value in data.items():
        if value is None:
            continue
        if getattr(config, field) != value:
            setattr(config, field, value)
            changed.append(field)
    config.updated_by = operator
    config.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return config, changed


def minutes_for_level(level: str, config: SlaConfig) -> int:
    return int(config.critical_minutes if level == "critical" else config.warning_minutes)


def apply_deadline(alert: DeviceAlert, config: SlaConfig) -> None:
    """在告警创建时写入 SLA 快照；created_at 可能由数据库默认值生成，兼容 None。"""
    minutes = minutes_for_level(alert.level, config)
    created = _utc(alert.created_at) or datetime.now(timezone.utc)
    alert.sla_minutes = minutes
    alert.sla_deadline_at = created + timedelta(minutes=minutes)
    alert.escalation_count = alert.escalation_count or 0
    alert.assignee = alert.assignee or None


# ==================== 判定 ====================


def _deadline(alert: DeviceAlert, config: SlaConfig) -> datetime:
    deadline = _utc(alert.sla_deadline_at)
    if deadline is not None:
        return deadline
    created = _utc(alert.created_at) or datetime.now(timezone.utc)
    minutes = int(alert.sla_minutes or minutes_for_level(alert.level, config))
    return created + timedelta(minutes=minutes)


def evaluate(alert: DeviceAlert, now: datetime, config: SlaConfig) -> Dict[str, Any]:
    """返回统一 SLA 口径；`sla_remaining_seconds` 对未处理是剩余（负数超出）。"""
    deadline = _deadline(alert, config)
    remaining = int((deadline - now).total_seconds())
    result: Dict[str, Any] = {
        "sla_state": STATE_PENDING,
        "sla_remaining_seconds": remaining,
        "sla_response_seconds": None,
    }
    if alert.status == "acked":
        acked_at = _utc(alert.acked_at)
        if acked_at is None:
            # 历史已确认行没有处置时间：明确归为 unknown，绝不算达标
            result["sla_state"] = STATE_UNKNOWN
            result["sla_remaining_seconds"] = None
        else:
            response = int((acked_at - (_utc(alert.created_at) or acked_at)).total_seconds())
            result["sla_response_seconds"] = max(0, response)
            result["sla_state"] = STATE_MET if acked_at <= deadline else STATE_LATE
            result["sla_remaining_seconds"] = int((deadline - acked_at).total_seconds())
    else:
        result["sla_state"] = STATE_OVERDUE if remaining < 0 else STATE_PENDING
    return result


def alert_to_out(alert: DeviceAlert, now: datetime, config: SlaConfig) -> AlertOut:
    out = AlertOut.model_validate(alert)
    values = evaluate(alert, now, config)
    out.sla_state = values["sla_state"]
    out.sla_remaining_seconds = values["sla_remaining_seconds"]
    out.sla_response_seconds = values["sla_response_seconds"]
    return out


def sla_filter_conditions(state: str, now: datetime) -> List[Any]:
    """列表筛选下推到 SQL，保证「筛选后的分页 total」与展示一致。

    `unknown` 专指历史已确认但无 acked_at 的行；它与 met 分开，避免被筛成达标。
    """
    if state == STATE_OVERDUE:
        return [DeviceAlert.status != "acked", DeviceAlert.sla_deadline_at < now]
    if state == STATE_PENDING:
        return [DeviceAlert.status != "acked", DeviceAlert.sla_deadline_at >= now]
    if state == STATE_MET:
        return [
            DeviceAlert.status == "acked",
            DeviceAlert.acked_at.is_not(None),
            DeviceAlert.acked_at <= DeviceAlert.sla_deadline_at,
        ]
    if state == STATE_LATE:
        return [
            DeviceAlert.status == "acked",
            DeviceAlert.acked_at.is_not(None),
            DeviceAlert.acked_at > DeviceAlert.sla_deadline_at,
        ]
    if state == STATE_UNKNOWN:
        return [
            DeviceAlert.status == "acked",
            (DeviceAlert.acked_at.is_(None) | DeviceAlert.sla_deadline_at.is_(None)),
        ]
    return []


async def count_overdue_open(session: AsyncSession) -> int:
    stmt = (
        select(func.count())
        .select_from(DeviceAlert)
        .where(DeviceAlert.status != "acked", DeviceAlert.sla_deadline_at < datetime.now(timezone.utc))
    )
    return int((await session.execute(stmt)).scalar_one())


# ==================== 超时升级 ====================


def _eligible(alert: DeviceAlert, now: datetime, config: SlaConfig) -> Optional[int]:
    """返回本轮应升级的步号；已到上限/未到触发时间返回 None。"""
    limit = int(config.max_escalations)
    current = int(alert.escalation_count or 0)
    if limit <= 0 or current >= limit:
        return None
    deadline = _deadline(alert, config)
    due_at = deadline + timedelta(minutes=int(config.escalation_interval_minutes) * current)
    if now < due_at:
        return None
    return current + 1


def _alert_payload(row: Any, device: Optional[Device]) -> Dict[str, Any]:
    return {
        "alertId": row.alert_id,
        "deviceId": row.device_id,
        "deviceName": device.name if device else row.device_id,
        "level": row.level,
        "metric": row.metric,
        "value": float(row.value),
        "threshold": float(row.threshold),
        "message": row.message,
        "aiGenerated": bool(row.ai_generated),
        "createdAt": _utc(row.created_at).isoformat() if row.created_at else None,
        "slaMinutes": row.sla_minutes,
        "assignee": row.assignee,
    }


async def _claim_escalation(
    session: AsyncSession, alert_id: str, step: int, now: datetime
) -> bool:
    """条件自增领取升级名额；并发/重启/多实例下同一步只会有一次成功。"""
    result = await session.execute(
        update(DeviceAlert)
        .where(
            DeviceAlert.alert_id == alert_id,
            DeviceAlert.status != "acked",
            DeviceAlert.escalation_count == step - 1,
        )
        .values(escalation_count=step, last_escalated_at=now)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0) == 1


_ESCALATION_COLUMNS = (
    DeviceAlert.alert_id,
    DeviceAlert.device_id,
    DeviceAlert.level,
    DeviceAlert.metric,
    DeviceAlert.value,
    DeviceAlert.threshold,
    DeviceAlert.message,
    DeviceAlert.ai_generated,
    DeviceAlert.created_at,
    DeviceAlert.sla_minutes,
    DeviceAlert.sla_deadline_at,
    DeviceAlert.assignee,
    DeviceAlert.escalation_count,
)


async def sweep_escalations(session_factory=None, now: Optional[datetime] = None) -> int:
    """巡检入口：找出到期的未处理告警，逐条领取名额并投递升级通知。

    返回本轮真正升级的条数。投递与计数在同一事务里落库（先写 delivery 再提交），
    因此：
    - 进程崩溃在提交前：计数不变，下一轮会重试，不丢；
    - 提交后发送失败：记录为 pending 由调度器续投，且计数已固化，不会重复发。
    """
    factory = session_factory or SessionFactory
    moment = now or datetime.now(timezone.utc)
    escalated = 0

    async with factory() as session:
        config = await get_or_create_config(session)
        snapshot = SimpleNamespace(
            critical_minutes=int(config.critical_minutes),
            warning_minutes=int(config.warning_minutes),
            max_escalations=int(config.max_escalations),
            escalation_interval_minutes=int(config.escalation_interval_minutes),
        )
        # 只取需要的列，后续每条告警用独立短事务，避免 commit 后 ORM 过期触发惰性加载
        candidates = list(
            (
                await session.execute(
                    select(*_ESCALATION_COLUMNS)
                    .where(DeviceAlert.status != "acked", DeviceAlert.sla_deadline_at <= moment)
                    .order_by(DeviceAlert.sla_deadline_at.asc())
                    .limit(SWEEP_BATCH)
                )
            ).all()
        )

    for alert in candidates:
        step = _eligible(alert, moment, snapshot)
        if step is None:
            continue

        delivery_ids: List[int] = []
        async with factory() as session:
            if not await _claim_escalation(session, alert.alert_id, step, moment):
                await session.rollback()
                continue

            device = await session.get(Device, alert.device_id)
            alert_payload = _alert_payload(alert, device)
            deadline = _deadline(alert, snapshot)
            extra = {
                "event": "alert.escalation",
                "escalation": {
                    "attempt": step,
                    "maxAttempts": int(snapshot.max_escalations),
                    "slaMinutes": int(alert.sla_minutes or minutes_for_level(alert.level, snapshot)),
                    "deadlineAt": deadline.isoformat(),
                    "overdueMinutes": max(0, int((moment - deadline).total_seconds() // 60)),
                },
            }

            notify_config = await notify_service.get_or_create_config(session)
            rule = None
            if notify_config.enabled:
                rule = await notify_service.resolve_rule(
                    session, notify_config, alert_payload, device.group_name if device else ""
                )
            group_name = device.group_name if device else ""
            if rule is not None:
                for channel in ("webhook", "smtp"):
                    if notify_service._channel_missing_reason(notify_config, rule, channel) is None:
                        row = notify_service._new_delivery(
                            alert_payload, channel=channel, rule=rule, group_name=group_name,
                            status="pending", kind="escalation", extra=extra,
                        )
                        session.add(row)
                        await session.flush()
                        delivery_ids.append(row.id)
            if not delivery_ids:
                error = "通知总开关关闭" if not notify_config.enabled else "未匹配任何可用通知规则"
                row = notify_service._new_delivery(
                    alert_payload, channel="rule", rule=rule, group_name=group_name,
                    status="suppressed", error=error, kind="escalation", extra=extra,
                )
                session.add(row)
            await session.commit()

            escalated += 1
            logger.warning(
                "超时升级 alert=%s device=%s level=%s attempt=%d/%d sla=%s min overdue=%d min",
                alert.alert_id, alert.device_id, alert.level, step,
                int(snapshot.max_escalations), extra["escalation"]["slaMinutes"],
                extra["escalation"]["overdueMinutes"],
            )

        for delivery_id in delivery_ids:
            await notify_service.process_delivery(delivery_id, factory)

    return escalated


# ==================== 值班交接 ====================


async def handover_open_alerts(
    session: AsyncSession,
    *,
    from_user: str,
    to: str,
    include_unassigned: bool = True,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """把未处理告警责任人从 `from_user` 交接到 `to`（落库，不重置 SLA）。

    范围：status != acked 且 (assignee == from_user 或 (include_unassigned 且 assignee 为空))。
    选择不重置计时与升级计数：SLA 衡量的是「告警从产生到被处理」的真实时长，
    换班不应让已超时的告警回到未超时，也不应重新触发升级轰炸；交接只换责任归属。
    """
    scope = [
        DeviceAlert.status != "acked",
        DeviceAlert.assignee == from_user
        if not include_unassigned
        else (DeviceAlert.assignee == from_user) | DeviceAlert.assignee.is_(None),
    ]
    rows = list((await session.execute(select(DeviceAlert).where(*scope))).scalars().all())
    if not rows:
        return {
            "count": 0, "window_start": None, "window_end": None,
            "max_level": "warning", "alerts": [],
        }

    window_start = min((_utc(row.created_at) for row in rows if row.created_at), default=None)
    window_end = max((_utc(row.created_at) for row in rows if row.created_at), default=None)
    max_level = "critical" if any(row.level == "critical" for row in rows) else "warning"
    alert_ids = [row.alert_id for row in rows]
    await session.execute(
        update(DeviceAlert)
        .where(DeviceAlert.alert_id.in_(alert_ids), DeviceAlert.status != "acked")
        .values(assignee=to)
        .execution_options(synchronize_session=False)
    )
    for row in rows:
        row.assignee = to
    await session.flush()
    return {
        "count": len(rows),
        "window_start": window_start,
        "window_end": window_end,
        "max_level": max_level,
        "alerts": alert_ids,
        "note": note,
    }



