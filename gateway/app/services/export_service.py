"""CSV 流式导出（v1.30.0）。

只做两件事：按既有筛选语义构造查询、按 keyset 分批写 CSV。
不在内存里累积结果，也不在前端拼 CSV。

筛选口径与列表接口保持一致：
- 记录：`device_id` 精确、`ts` 闭区间、可选设备分组；
- 告警：`device_id` 精确、`level` / `status`、`created_at` 闭区间、可选设备分组。
"""

import csv
import io
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceAlert, SensorRecord
from app.services import tag_service

# 每批 2000 行：单批内存可控，又能把数据库往返次数压到 100 次/20 万行
BATCH_SIZE = 2000

RECORD_HEADERS = [
    "记录ID",
    "设备ID",
    "设备名称",
    "设备分组",
    "温度(°C)",
    "湿度(%)",
    "电压(V)",
    "信号(dBm)",
    "电量(%)",
    "序号",
    "上报时间(UTC)",
]

ALERT_HEADERS = [
    "告警ID",
    "设备ID",
    "设备名称",
    "设备分组",
    "级别",
    "指标",
    "数值",
    "阈值",
    "告警内容",
    "AI生成",
    "状态",
    "确认人",
    "确认备注",
    "确认时间(UTC)",
    "创建时间(UTC)",
]

# Excel 会把以这些字符开头的单元格当公式执行，导出前统一转义（CSV 注入防护）
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _identifier(value: Any) -> str:
    """设备 ID / 告警 ID：纯数字且以 0 开头时用 Excel 文本公式保留前导零。"""
    text = _text(value)
    if text.isdigit() and text.startswith("0"):
        return f'="{text}"'
    return text


def _number(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _utc(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _records_filter(
    device_id: Optional[str],
    group_name: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
    tags: Optional[Sequence[str]] = None,
):
    conditions = []
    if device_id:
        conditions.append(SensorRecord.device_id == device_id)
    if group_name:
        conditions.append(Device.group_name == group_name)
    if tags:
        conditions.append(
            SensorRecord.device_id.in_(tag_service.device_ids_with_tags(tags))
        )
    if start:
        conditions.append(SensorRecord.ts >= start)
    if end:
        conditions.append(SensorRecord.ts <= end)
    return conditions


def _alerts_filter(
    device_id: Optional[str],
    group_name: Optional[str],
    level: Optional[str],
    status: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
    tags: Optional[Sequence[str]] = None,
):
    conditions = []
    if device_id:
        conditions.append(DeviceAlert.device_id == device_id)
    if group_name:
        conditions.append(Device.group_name == group_name)
    if tags:
        conditions.append(
            DeviceAlert.device_id.in_(tag_service.device_ids_with_tags(tags))
        )
    if level:
        conditions.append(DeviceAlert.level == level)
    if status:
        conditions.append(DeviceAlert.status == status)
    if start:
        conditions.append(DeviceAlert.created_at >= start)
    if end:
        conditions.append(DeviceAlert.created_at <= end)
    return conditions


async def count_records(
    session: AsyncSession,
    device_id: Optional[str],
    group_name: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
    tags: Optional[Sequence[str]] = None,
) -> int:
    stmt = select(func.count(SensorRecord.id)).select_from(SensorRecord)
    conditions = _records_filter(device_id, group_name, start, end, tags)
    if group_name:
        stmt = stmt.join(Device, Device.device_id == SensorRecord.device_id)
    if conditions:
        stmt = stmt.where(*conditions)
    return int((await session.execute(stmt)).scalar_one())


async def count_alerts(
    session: AsyncSession,
    device_id: Optional[str],
    group_name: Optional[str],
    level: Optional[str],
    status: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
    tags: Optional[Sequence[str]] = None,
) -> int:
    stmt = select(func.count(DeviceAlert.id)).select_from(DeviceAlert)
    conditions = _alerts_filter(device_id, group_name, level, status, start, end, tags)
    if group_name:
        stmt = stmt.join(Device, Device.device_id == DeviceAlert.device_id)
    if conditions:
        stmt = stmt.where(*conditions)
    return int((await session.execute(stmt)).scalar_one())


async def stream_records(
    session: AsyncSession,
    device_id: Optional[str],
    group_name: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
    state: Dict[str, int],
    tags: Optional[Sequence[str]] = None,
) -> AsyncIterator[str]:
    """按时间正序 keyset 分批导出；state["rows"] 记录实际写出行数。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    buffer.write("\ufeff")
    writer.writerow(RECORD_HEADERS)
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    base_conditions = _records_filter(device_id, group_name, start, end, tags)
    last_ts: Optional[datetime] = None
    last_id: Optional[int] = None
    exported = 0

    while True:
        conditions = list(base_conditions)
        if last_id is not None and last_ts is not None:
            conditions.append(
                or_(
                    SensorRecord.ts > last_ts,
                    and_(SensorRecord.ts == last_ts, SensorRecord.id > last_id),
                )
            )
        stmt = (
            select(
                SensorRecord.id,
                SensorRecord.device_id,
                Device.name,
                Device.group_name,
                SensorRecord.temperature,
                SensorRecord.humidity,
                SensorRecord.voltage,
                SensorRecord.rssi,
                SensorRecord.battery,
                SensorRecord.seq,
                SensorRecord.ts,
            )
            .select_from(SensorRecord)
            .outerjoin(Device, Device.device_id == SensorRecord.device_id)
        )
        if conditions:
            stmt = stmt.where(*conditions)
        stmt = stmt.order_by(SensorRecord.ts.asc(), SensorRecord.id.asc()).limit(BATCH_SIZE)
        rows = (await session.execute(stmt)).all()
        if not rows:
            break

        for row in rows:
            writer.writerow(
                [
                    row[0],
                    _identifier(row[1]),
                    _text(row[2]),
                    _text(row[3]),
                    _number(row[4]),
                    _number(row[5]),
                    _number(row[6]),
                    _number(row[7]),
                    _number(row[8]),
                    row[9],
                    _utc(row[10]),
                ]
            )
        last_id = rows[-1][0]
        last_ts = rows[-1][10]
        exported += len(rows)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

    state["rows"] = exported


async def stream_alerts(
    session: AsyncSession,
    device_id: Optional[str],
    group_name: Optional[str],
    level: Optional[str],
    status: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
    state: Dict[str, int],
    tags: Optional[Sequence[str]] = None,
) -> AsyncIterator[str]:
    """按创建时间正序 keyset 分批导出；state["rows"] 记录实际写出行数。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    buffer.write("\ufeff")
    writer.writerow(ALERT_HEADERS)
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    base_conditions = _alerts_filter(device_id, group_name, level, status, start, end, tags)
    last_created: Optional[datetime] = None
    last_id: Optional[int] = None
    exported = 0

    while True:
        conditions = list(base_conditions)
        if last_id is not None and last_created is not None:
            conditions.append(
                or_(
                    DeviceAlert.created_at > last_created,
                    and_(DeviceAlert.created_at == last_created, DeviceAlert.id > last_id),
                )
            )
        stmt = (
            select(
                DeviceAlert.alert_id,
                DeviceAlert.device_id,
                Device.name,
                Device.group_name,
                DeviceAlert.level,
                DeviceAlert.metric,
                DeviceAlert.value,
                DeviceAlert.threshold,
                DeviceAlert.message,
                DeviceAlert.ai_generated,
                DeviceAlert.status,
                DeviceAlert.acked_by,
                DeviceAlert.ack_note,
                DeviceAlert.acked_at,
                DeviceAlert.created_at,
                DeviceAlert.id,
            )
            .select_from(DeviceAlert)
            .outerjoin(Device, Device.device_id == DeviceAlert.device_id)
        )
        if conditions:
            stmt = stmt.where(*conditions)
        stmt = stmt.order_by(DeviceAlert.created_at.asc(), DeviceAlert.id.asc()).limit(
            BATCH_SIZE
        )
        rows = (await session.execute(stmt)).all()
        if not rows:
            break

        for row in rows:
            writer.writerow(
                [
                    _identifier(row[0]),
                    _identifier(row[1]),
                    _text(row[2]),
                    _text(row[3]),
                    row[4],
                    row[5],
                    _number(row[6]),
                    _number(row[7]),
                    _text(row[8]),
                    "是" if row[9] else "否",
                    row[10],
                    _text(row[11]),
                    _text(row[12]),
                    _utc(row[13]),
                    _utc(row[14]),
                ]
            )
        last_id = rows[-1][15]
        last_created = rows[-1][14]
        exported += len(rows)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

    state["rows"] = exported


def build_filename(kind: str, start: Optional[datetime], end: Optional[datetime]) -> str:
    """导出文件名：对象 + UTC 时间范围；未指定范围时用 all 标记，避免误导。"""
    today = datetime.now(timezone.utc).date().isoformat()

    def _day(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date().isoformat()

    if start and end:
        return f"{kind}_{_day(start)}_{_day(end)}.csv"
    if start:
        return f"{kind}_{_day(start)}_{today}.csv"
    return f"{kind}_all_{today}.csv"


def audit_detail(
    filters: Dict[str, Any], rows: int, duration_ms: int, filename: str
) -> Dict[str, Any]:
    """审计只记录筛选条件、行数、耗时与文件名，绝不写导出内容。"""
    return {
        "filters": filters,
        "rows": rows,
        "duration_ms": duration_ms,
        "filename": filename,
    }
