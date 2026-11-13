"""维护窗口时间与范围语义（v1.39.0）。

时间统一用 UTC：
- 一次性窗口存 `starts_at` / `ends_at`（UTC），半开区间 `[start, end)`；
- 周窗口存 `recurrence = {"weekday": 0-6, "start": "HH:MM", "duration_minutes": N}`，
  `weekday` 0=周一、6=周日，`start` 为 UTC 时间。

为什么用 UTC 而不是本地时区：全库时间列本来就是 UTC，按 UTC 计算不依赖进程/部署机时区，
也没有 DST 边界（中国无 DST，但部署可能在其他区域）；代价是界面需标注 UTC。
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from app import rule_conditions

MODE_ONCE = "once"
MODE_WEEKLY = "weekly"
MODES = (MODE_ONCE, MODE_WEEKLY)
STATUS_SCHEDULED = "scheduled"
STATUS_ENDED = "ended"
MAX_DURATION_MINUTES = 7 * 24 * 60


class MaintenanceError(ValueError):
    """维护窗口参数非法：路由层转成可读 400。"""


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """把 SQLite 读出的 naive datetime 视为 UTC（与 SLA 服务口径一致）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_scope(raw: Any) -> Optional[Dict[str, Any]]:
    """复用告警规则条件树校验；None 表示全部设备（显式全局）。"""
    if raw is None:
        return None
    try:
        return rule_conditions.normalize(raw)
    except rule_conditions.ConditionError as exc:
        raise MaintenanceError(f"窗口范围不合法：{exc}") from exc


def _parse_hhmm(text: Any) -> time:
    if not isinstance(text, str) or ":" not in text:
        raise MaintenanceError("周期开始时间必须是 HH:MM（UTC）")
    try:
        hour, minute = text.split(":", 1)
        parsed = time(hour=int(hour), minute=int(minute))
    except (TypeError, ValueError) as exc:
        raise MaintenanceError("周期开始时间必须是 HH:MM（UTC）") from exc
    return parsed


def normalize_recurrence(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise MaintenanceError("周期窗口缺少 recurrence 配置")
    extra = set(raw) - {"weekday", "start", "duration_minutes"}
    if extra:
        raise MaintenanceError(f"recurrence 包含未知字段：{'、'.join(sorted(extra))}")
    weekday = raw.get("weekday")
    if not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
        raise MaintenanceError("weekday 必须是 0（周一）到 6（周日）")
    parsed = _parse_hhmm(raw.get("start"))
    duration = raw.get("duration_minutes")
    if not isinstance(duration, int) or isinstance(duration, bool) or not 1 <= duration <= MAX_DURATION_MINUTES:
        raise MaintenanceError(f"duration_minutes 必须是 1 到 {MAX_DURATION_MINUTES} 的整数")
    return {
        "weekday": weekday,
        "start": f"{parsed.hour:02d}:{parsed.minute:02d}",
        "duration_minutes": duration,
    }


def _utc_datetime(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Occurrence:
    start: datetime
    end: datetime


def occurrence_for(window: Any, now: datetime) -> Optional[Occurrence]:
    """返回 `now` 命中的那次窗口发生；不命中 / 已结束返回 None。

    支持跨天（时长 > 当天剩余时间）与跨周边界：回看 8 天找出仍覆盖 now 的发生。
    """
    now = as_utc(now) or datetime.now(timezone.utc)
    if getattr(window, "status", STATUS_SCHEDULED) == STATUS_ENDED:
        return None

    if getattr(window, "mode", MODE_ONCE) == MODE_ONCE:
        start = as_utc(getattr(window, "starts_at", None))
        end = as_utc(getattr(window, "ends_at", None))
        if start and end and start <= now < end:
            return Occurrence(start, end)
        return None

    recurrence = getattr(window, "recurrence", None) or {}
    try:
        clock = _parse_hhmm(recurrence.get("start"))
        weekday = int(recurrence["weekday"])
        duration = int(recurrence["duration_minutes"])
    except (KeyError, TypeError, ValueError):
        return None

    best: Optional[Occurrence] = None
    for back in range(0, 8):
        day = (now - timedelta(days=back)).date()
        if day.weekday() != weekday:
            continue
        start = _utc_datetime(day, clock)
        end = start + timedelta(minutes=duration)
        if start <= now < end and (best is None or start > best.start):
            best = Occurrence(start, end)
    return best


def next_start(window: Any, now: datetime) -> Optional[datetime]:
    """下一次窗口开始时间（严格大于 now）；已结束或一次性窗口已过返回 None。"""
    now = as_utc(now) or datetime.now(timezone.utc)
    if getattr(window, "status", STATUS_SCHEDULED) == STATUS_ENDED:
        return None

    if getattr(window, "mode", MODE_ONCE) == MODE_ONCE:
        start = as_utc(getattr(window, "starts_at", None))
        return start if start and start > now else None

    recurrence = getattr(window, "recurrence", None) or {}
    try:
        clock = _parse_hhmm(recurrence.get("start"))
        weekday = int(recurrence["weekday"])
        duration = int(recurrence["duration_minutes"])
    except (KeyError, TypeError, ValueError):
        return None

    # 当前发生内部时，下一次是下一个周期；否则找最近未来的一次
    current = occurrence_for(window, now)
    if current is not None:
        return current.start + timedelta(days=7)
    for ahead in range(0, 9):
        day = (now + timedelta(days=ahead)).date()
        if day.weekday() != weekday:
            continue
        start = _utc_datetime(day, clock)
        if start > now:
            return start
    return None


def validate_once_times(starts_at: Optional[datetime], ends_at: Optional[datetime]) -> Tuple[datetime, datetime]:
    start = as_utc(starts_at)
    end = as_utc(ends_at)
    if start is None or end is None:
        raise MaintenanceError("一次性窗口必须提供 starts_at 与 ends_at")
    if end <= start:
        raise MaintenanceError("结束时间必须晚于开始时间")
    return start, end
