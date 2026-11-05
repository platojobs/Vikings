"""告警服务：规则判定 → AI 文案 → 落库 → 返回待发布的 MQTT 报文。"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.diagnosis import generate_alert_text
from app.config import get_settings
from app.models import DeviceAlert
from app.rules import evaluate
from app.schemas import SensorReportMessage
from app.services import sla_service

logger = logging.getLogger(__name__)
settings = get_settings()


async def _recent_alert_exists(session: AsyncSession, device_id: str, metric: str) -> bool:
    """告警抑制：同设备同指标在抑制窗口内已有告警则不再重复产生。

    电池电压这类缓慢变化的指标，如果每分钟都刷一条告警会把看板淹没，
    所以默认 5 分钟窗口内同类告警只保留第一条。
    """
    since = datetime.now(timezone.utc) - timedelta(seconds=settings.alert_suppress_seconds)
    stmt = (
        select(func.count())
        .select_from(DeviceAlert)
        .where(
            DeviceAlert.device_id == device_id,
            DeviceAlert.metric == metric,
            DeviceAlert.created_at >= since,
        )
    )
    return int((await session.execute(stmt)).scalar_one()) > 0


async def handle_report_alerts(
    session: AsyncSession,
    report: SensorReportMessage,
    device_name: str,
    group_name: Optional[str] = None,
) -> List[Dict]:
    """对一条上报执行完整告警链路，返回需要发布到 device/system/alert 的报文列表。

    v1.34.0：判定读取 alert_rule 规则缓存（支持分组/设备范围），命中结果带
    matched_rule 快照并原样返回给 MQTT；规则变更无需重启即生效。
    """
    hits = evaluate(report, device_id=report.device_id, group_name=group_name)
    if not hits:
        return []

    # 先剔除被抑制的指标，再并发生成文案（LLM 是网络 IO，串行会拖慢消费）
    candidates = []
    for hit in hits:
        if await _recent_alert_exists(session, report.device_id, hit["metric"]):
            logger.debug("告警被抑制: device=%s metric=%s", report.device_id, hit["metric"])
            continue
        candidates.append(hit)

    if not candidates:
        return []

    texts = await asyncio.gather(
        *(generate_alert_text(report.device_id, hit) for hit in candidates)
    )

    messages: List[Dict] = []
    now = datetime.now(timezone.utc)
    # SLA 时限在创建时快照：之后修改配置只影响新告警，历史判定可复盘
    sla_config = await sla_service.get_or_create_config(session)
    for hit, (text, ai_generated) in zip(candidates, texts):
        # 规则快照：规则后续修改/删除也不影响这条历史告警的解释
        matched_rule = {
            "rule_id": hit.get("rule_id"),
            "rule_name": hit.get("rule_name"),
            "op": hit.get("op"),
            "threshold": float(hit["threshold"]),
            "level": hit["level"],
            "value": float(hit["value"]),
            "consecutive": int(hit.get("consecutive_count") or 1),
        }
        alert = DeviceAlert(
            alert_id="alt-" + uuid.uuid4().hex[:12],
            device_id=report.device_id,
            level=hit["level"],
            metric=hit["metric"],
            value=hit["value"],
            threshold=hit["threshold"],
            message=text,
            ai_generated=ai_generated,
            status="open",
            matched_rule=matched_rule,
            created_at=now,
        )
        sla_service.apply_deadline(alert, sla_config)
        session.add(alert)
        messages.append(
            {
                "alertId": alert.alert_id,
                "deviceId": report.device_id,
                "deviceName": device_name,
                "level": alert.level,
                "metric": alert.metric,
                "value": float(alert.value),
                "threshold": float(alert.threshold),
                "message": alert.message,
                "aiGenerated": alert.ai_generated,
                "matchedRule": matched_rule,
                "ts": int(now.timestamp() * 1000),
            }
        )
        logger.warning(
            "生成告警 device=%s metric=%s level=%s value=%s threshold=%s rule=%s ai=%s",
            report.device_id, hit["metric"], hit["level"], hit["value"], hit["threshold"],
            hit.get("rule_name"), ai_generated,
        )

    await session.flush()
    return messages


async def list_alerts(
    session: AsyncSession,
    device_id: Optional[str],
    level: Optional[str],
    status: Optional[str],
    page: int,
    page_size: int,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    sla_state: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Tuple[List[DeviceAlert], int]:
    """按创建时间倒序分页；`start` / `end` 为 created_at 闭区间（v1.30.0）。

    导出接口复用同一套过滤条件，保证「列表看到的范围」与「导出的范围」一致。
    """
    stmt = select(DeviceAlert)
    count_stmt = select(func.count()).select_from(DeviceAlert)

    if device_id:
        stmt = stmt.where(DeviceAlert.device_id == device_id)
        count_stmt = count_stmt.where(DeviceAlert.device_id == device_id)
    if level:
        stmt = stmt.where(DeviceAlert.level == level)
        count_stmt = count_stmt.where(DeviceAlert.level == level)
    if status:
        stmt = stmt.where(DeviceAlert.status == status)
        count_stmt = count_stmt.where(DeviceAlert.status == status)
    if start:
        stmt = stmt.where(DeviceAlert.created_at >= start)
        count_stmt = count_stmt.where(DeviceAlert.created_at >= start)
    if end:
        stmt = stmt.where(DeviceAlert.created_at <= end)
        count_stmt = count_stmt.where(DeviceAlert.created_at <= end)
    # v1.33.0：SLA 状态筛选（尤其「已超时」）下推到 SQL，分页 total 与展示一致
    if sla_state:
        for condition in sla_service.sla_filter_conditions(
            sla_state, now or datetime.now(timezone.utc)
        ):
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

    stmt = stmt.order_by(DeviceAlert.created_at.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    total = int((await session.execute(count_stmt)).scalar_one())
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total


async def ack_alert(
    session: AsyncSession,
    alert_id: str,
    operator: str = "system",
    note: Optional[str] = None,
) -> Optional[DeviceAlert]:
    """确认单条告警，记录「谁、何时、为什么」。

    已是 acked 的行保持幂等：不覆盖首次确认人/时间/备注，
    避免重复点击或旧客户端重试改写处置证据。
    """
    stmt = select(DeviceAlert).where(DeviceAlert.alert_id == alert_id)
    alert = (await session.execute(stmt)).scalars().first()
    if alert is None:
        return None
    if alert.status != "acked":
        alert.status = "acked"
        alert.acked_by = operator
        alert.acked_at = datetime.now(timezone.utc)
        alert.ack_note = note
    await session.flush()
    return alert


async def bulk_ack_alerts(
    session: AsyncSession,
    alert_ids: List[str],
    operator: str = "system",
    note: Optional[str] = None,
) -> Dict[str, object]:
    """批量确认告警，逐类返回结果，允许部分失败。

    语义：
    - 重复 ID 先去重，`requested` 为去重后的条数；
    - open → acked 计入 `acked`，同一批写入相同的确认人/时间/可选备注；
    - 已是 acked 计入 `already_acked`（幂等成功，不当作失败，也不改写既有处置信息）；
    - 不存在的 ID 计入 `not_found`，不抛 404，避免一条脏 ID 让整批失败。

    调用方负责提交事务与审计；本函数只 flush，便于复用现有告警落库流程。
    """
    unique_ids = list(dict.fromkeys(alert_id for alert_id in alert_ids if alert_id))
    if not unique_ids:
        return {"acked": [], "already_acked": [], "not_found": [], "requested": 0}

    stmt = select(DeviceAlert).where(DeviceAlert.alert_id.in_(unique_ids))
    rows = list((await session.execute(stmt)).scalars().all())
    by_id = {row.alert_id: row for row in rows}
    acked_at = datetime.now(timezone.utc)

    acked: List[str] = []
    already_acked: List[str] = []
    not_found: List[str] = []
    for alert_id in unique_ids:
        row = by_id.get(alert_id)
        if row is None:
            not_found.append(alert_id)
        elif row.status == "acked":
            already_acked.append(alert_id)
        else:
            row.status = "acked"
            row.acked_by = operator
            row.acked_at = acked_at
            row.ack_note = note
            acked.append(alert_id)

    await session.flush()
    return {
        "acked": acked,
        "already_acked": already_acked,
        "not_found": not_found,
        "requested": len(unique_ids),
    }


async def count_open_alerts(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(DeviceAlert).where(DeviceAlert.status == "open")
    return int((await session.execute(stmt)).scalar_one())


async def open_alert_counts_by_device(
    session: AsyncSession, device_ids: List[str]
) -> Dict[str, int]:
    """按设备聚合未处理告警数。

    移动看板一次只加载一页设备，这里按当前页设备 ID 批量聚合，
    避免依赖 `GET /api/alerts?pageSize=50` 的前 50 条，也避免 N+1 查询。
    """
    if not device_ids:
        return {}
    stmt = (
        select(DeviceAlert.device_id, func.count())
        .where(DeviceAlert.status == "open", DeviceAlert.device_id.in_(device_ids))
        .group_by(DeviceAlert.device_id)
    )
    return {device_id: int(count) for device_id, count in (await session.execute(stmt)).all()}


async def alert_summary(session: AsyncSession) -> Dict:
    """告警中心顶部统计：未处理数 / 未处理严重数 / 今日新增数。"""
    open_stmt = select(func.count()).select_from(DeviceAlert).where(DeviceAlert.status == "open")
    critical_stmt = open_stmt.where(DeviceAlert.level == "critical")
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_stmt = select(func.count()).select_from(DeviceAlert).where(
        DeviceAlert.created_at >= today_start
    )
    return {
        "open": int((await session.execute(open_stmt)).scalar_one()),
        "critical_open": int((await session.execute(critical_stmt)).scalar_one()),
        "today": int((await session.execute(today_stmt)).scalar_one()),
        # v1.33.0：未处理中已超出响应时限的条数，便于首页一眼看出「欠账」
        "overdue_open": await sla_service.count_overdue_open(session),
    }
