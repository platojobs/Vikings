"""电池健康与续航看板（v1.32.0）。

只有一套口径：窗口内真实 `sensor_record` 的电量数据。
- 最新电量 / 电压 / 上报时间取窗口内每台设备最后一条记录；
- 衰减速度按「每日平均电量」做最小二乘拟合，单位为百分点/天，仅正衰减时给出；
- 预计剩余天数 = 最新电量到严重阈值（15%）的距离 / 衰减速度；
- 窗口内没有任何上报的设备记为 `unknown`，不做插值或臆测补值。

风险分层同时看「当前电量」与「预计天数」：电量还高但掉得极快的设备同样需要
提前换电，因此只要任一条件触碰阈值就升级风险等级。
"""

from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, SensorRecord
from app.schemas import (
    BatteryGroupSummaryOut,
    BatteryHealthOut,
    BatteryRiskDeviceOut,
    BatteryTrendPointOut,
)
from app.services.alert_service import open_alert_counts_by_device

# 分层阈值：百分比与预计天数都参与判定
BATTERY_WARNING_PERCENT = 30
BATTERY_CRITICAL_PERCENT = 15
BATTERY_WARNING_DAYS = 30.0
BATTERY_CRITICAL_DAYS = 7.0

# 日斜率低于该值视为「基本不耗电」，避免量化噪声被放大成巨大的剩余天数
MIN_DRAIN_PER_DAY = 0.01

RISK_ORDER = {"critical": 0, "warning": 1}
RISK_ALERT = {"critical", "warning"}


def _day_bucket(session: AsyncSession):
    """按天分桶：PostgreSQL 用 date_trunc，SQLite 用 strftime（测试同口径）。"""
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return func.strftime("%Y-%m-%d", SensorRecord.ts)
    # 先转成 UTC 的时间戳再截断：PostgreSQL 的 date_trunc 按会话时区取零点，
    # 若会话是 Asia/Shanghai，返回的零点再按 UTC 解读会整体倒退一天。
    return func.date_trunc("day", func.timezone("UTC", SensorRecord.ts))


def _day_key(value) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite 存的是 naive UTC；PostgreSQL 返回带时区。统一按 UTC 返回。"""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _drain_per_day(points: List[Tuple[date, float]]) -> Optional[float]:
    """对每日平均电量做最小二乘拟合，返回正衰减速度（百分点/天）。"""
    if len(points) < 2:
        return None
    ordered = sorted(points)
    base_day = ordered[0][0]
    xs = [(day - base_day).days for day, _ in ordered]
    ys = [value for _, value in ordered]
    if len(set(xs)) < 2:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    # 电量随天数下降时斜率为负，取反后得到「每天掉多少个百分点」
    drain = -sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
    if drain < MIN_DRAIN_PER_DAY:
        return None
    return round(drain, 2)


def _days_to_critical(battery: Optional[int], drain_per_day: Optional[float]) -> Optional[float]:
    if battery is None:
        return None
    if battery <= BATTERY_CRITICAL_PERCENT:
        return 0.0
    if drain_per_day is None or drain_per_day <= 0:
        return None
    return round((battery - BATTERY_CRITICAL_PERCENT) / drain_per_day, 1)


def _risk(battery: Optional[int], days_to_critical: Optional[float]) -> str:
    if battery is None:
        return "unknown"
    if battery <= BATTERY_CRITICAL_PERCENT or (
        days_to_critical is not None and days_to_critical <= BATTERY_CRITICAL_DAYS
    ):
        return "critical"
    if battery <= BATTERY_WARNING_PERCENT or (
        days_to_critical is not None and days_to_critical <= BATTERY_WARNING_DAYS
    ):
        return "warning"
    return "healthy"


async def _device_rows(session: AsyncSession, group: Optional[str]):
    stmt = select(Device.device_id, Device.name, Device.group_name, Device.status)
    if group:
        stmt = stmt.where(Device.group_name == group)
    return (await session.execute(stmt.order_by(Device.device_id))).all()


async def _daily_battery(
    session: AsyncSession, group: Optional[str], window_start: datetime
) -> Dict[str, List[Tuple[date, float]]]:
    day_expr = _day_bucket(session)
    stmt = (
        select(
            SensorRecord.device_id,
            day_expr.label("day"),
            func.avg(SensorRecord.battery),
        )
        .join(Device, Device.device_id == SensorRecord.device_id)
        .where(SensorRecord.ts >= window_start)
        .group_by(SensorRecord.device_id, day_expr)
    )
    if group:
        stmt = stmt.where(Device.group_name == group)
    series: Dict[str, List[Tuple[date, float]]] = {}
    for device_id, day, avg_battery in (await session.execute(stmt)).all():
        series.setdefault(device_id, []).append((_day_key(day), float(avg_battery)))
    return series


async def _latest_records(
    session: AsyncSession, group: Optional[str], window_start: datetime
) -> Dict[str, SensorRecord]:
    latest = (
        select(
            SensorRecord.device_id,
            func.max(SensorRecord.ts).label("max_ts"),
        )
        .where(SensorRecord.ts >= window_start)
        .group_by(SensorRecord.device_id)
        .subquery()
    )
    stmt = select(SensorRecord).join(
        latest,
        and_(
            SensorRecord.device_id == latest.c.device_id,
            SensorRecord.ts == latest.c.max_ts,
        ),
    )
    if group:
        stmt = stmt.join(Device, Device.device_id == SensorRecord.device_id).where(
            Device.group_name == group
        )
    # 同一设备同一时间戳可能有重复 seq（QoS1 重投），保留 seq 最大的一条
    latest_by_device: Dict[str, SensorRecord] = {}
    for record in (await session.execute(stmt.order_by(SensorRecord.seq))).scalars():
        latest_by_device[record.device_id] = record
    return latest_by_device


def _fleet_trend(series: Dict[str, List[Tuple[date, float]]]) -> List[BatteryTrendPointOut]:
    """按天汇总整机队平均电量：对当日有数据的设备取简单平均。

    简单平均避免个别高频上报设备主导趋势；`samples` 表示当日参与计算的设备数。
    """
    per_day: Dict[date, Tuple[float, int]] = {}
    for points in series.values():
        for day, value in points:
            total, count = per_day.get(day, (0.0, 0))
            per_day[day] = (total + value, count + 1)
    return [
        BatteryTrendPointOut(
            day_start=datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
            avg_battery=round(total / count, 1),
            samples=count,
        )
        for day, (total, count) in sorted(per_day.items())
        if count
    ]


async def battery_health(
    session: AsyncSession,
    group: Optional[str] = None,
    days: int = 7,
    limit: int = 20,
) -> BatteryHealthOut:
    now = datetime.now(timezone.utc)
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days - 1
    )

    devices = await _device_rows(session, group)
    if not devices:
        return BatteryHealthOut(
            device_total=0,
            battery_known=0,
            critical=0,
            warning=0,
            healthy=0,
            unknown=0,
            avg_battery=None,
            warning_percent=BATTERY_WARNING_PERCENT,
            critical_percent=BATTERY_CRITICAL_PERCENT,
            window_days=days,
            window_start=window_start,
            trend=[],
            groups=[],
            watchlist=[],
            watchlist_total=0,
        )

    device_ids = [row.device_id for row in devices]
    series = await _daily_battery(session, group, window_start)
    latest = await _latest_records(session, group, window_start)
    alert_counts = await open_alert_counts_by_device(session, device_ids)

    items: List[BatteryRiskDeviceOut] = []
    for row in devices:
        record = latest.get(row.device_id)
        battery = int(record.battery) if record is not None else None
        drain = _drain_per_day(series.get(row.device_id, []))
        days_left = _days_to_critical(battery, drain)
        items.append(
            BatteryRiskDeviceOut(
                device_id=row.device_id,
                name=row.name,
                group_name=row.group_name,
                status=row.status,
                battery=battery,
                voltage=round(float(record.voltage), 2) if record is not None else None,
                last_report_at=_as_utc(record.ts) if record is not None else None,
                drain_per_day=drain,
                days_to_critical=days_left,
                risk=_risk(battery, days_left),
                open_alert_count=alert_counts.get(row.device_id, 0),
            )
        )

    counter = {"critical": 0, "warning": 0, "healthy": 0, "unknown": 0}
    for item in items:
        counter[item.risk] += 1
    known = [item.battery for item in items if item.battery is not None]
    avg_battery = round(sum(known) / len(known), 1) if known else None

    group_map: Dict[str, List[BatteryRiskDeviceOut]] = {}
    for item in items:
        group_map.setdefault(item.group_name, []).append(item)
    groups = []
    for group_name, group_items in sorted(group_map.items()):
        group_known = [item.battery for item in group_items if item.battery is not None]
        groups.append(
            BatteryGroupSummaryOut(
                group_name=group_name,
                total=len(group_items),
                critical=sum(1 for item in group_items if item.risk == "critical"),
                warning=sum(1 for item in group_items if item.risk == "warning"),
                healthy=sum(1 for item in group_items if item.risk == "healthy"),
                unknown=sum(1 for item in group_items if item.risk == "unknown"),
                avg_battery=round(sum(group_known) / len(group_known), 1)
                if group_known
                else None,
            )
        )

    at_risk = [item for item in items if item.risk in RISK_ALERT]
    at_risk.sort(
        key=lambda item: (
            RISK_ORDER[item.risk],
            item.days_to_critical if item.days_to_critical is not None else float("inf"),
            item.battery if item.battery is not None else 999,
            item.device_id,
        )
    )

    return BatteryHealthOut(
        device_total=len(items),
        battery_known=len(known),
        critical=counter["critical"],
        warning=counter["warning"],
        healthy=counter["healthy"],
        unknown=counter["unknown"],
        avg_battery=avg_battery,
        warning_percent=BATTERY_WARNING_PERCENT,
        critical_percent=BATTERY_CRITICAL_PERCENT,
        window_days=days,
        window_start=window_start,
        trend=_fleet_trend(series),
        groups=groups,
        watchlist=at_risk[:limit],
        watchlist_total=len(at_risk),
    )
