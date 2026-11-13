"""设备影子服务（v1.36.0 / v1.37.0）。

职责：期望状态写入、上报状态接收、同步状态派生、批量下发、差异计算、
不可变版本快照与完整快照精确回滚。

设计要点：
- 一行一设备，主键即 device_id；不存在「孤儿影子」：设备删除时由 device_service 显式清理；
- desired 写入采用 merge 语义：null 表示清除该键的期望；
- reported 写入是 merge（设备只上报本次变化的字段）；
- 同步状态派生而非落库：以 `desired` 与 `reported` 的差集 + `device.status` 联合判断，
  避免与 MQTT 在线状态双写不一致；
- 版本号：每次 desired 真正发生变化（patch 不为空）才 +1，纯幂等 patch 不递增；
  设备上报时携带 applied version，推进 `reported_version`，用于检测「设备端消费落后」。

权限：desired 与 reported 的 REST 写操作均为 admin/operator；设备模拟器使用对应 access token。
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceShadow, DeviceShadowRevision

logger = logging.getLogger(__name__)

# 单条 patch / reported 上限：过大载荷会让差异解释与同步状态失去可读性
MAX_KEYS = 32
MAX_KEY_LEN = 32
# 单字段值序列化后长度上限：避免某个 key 写入大段 JSON 字符串
MAX_VALUE_JSON_LEN = 512
# 单次批量下发上限
MAX_BATCH = 200

# 值允许的类型：字符串 / 数字 / 布尔 / null；不允许嵌套对象或数组
_ALLOWED_VALUE_TYPES = (str, int, float, bool, type(None))


class ShadowValidationError(ValueError):
    """期望/上报数据不合法；路由层转换为 400。"""


class ShadowRevisionNotFound(ValueError):
    """目标影子版本没有快照；路由层转换为 404。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_patch(patch: Dict[str, Any], *, field: str) -> None:
    """校验 patch / reported 字典；非法时抛 ShadowValidationError。"""
    if not isinstance(patch, dict):
        raise ShadowValidationError(f"{field} 必须是对象")
    if len(patch) > MAX_KEYS:
        raise ShadowValidationError(f"{field} 单次最多 {MAX_KEYS} 个键（当前 {len(patch)}）")
    for key, value in patch.items():
        if not isinstance(key, str):
            raise ShadowValidationError(f"{field} 的键必须是字符串")
        if not key or len(key) > MAX_KEY_LEN:
            raise ShadowValidationError(
                f"{field} 的键长度需在 1..{MAX_KEY_LEN} 之间（当前键={key!r}）"
            )
        if not isinstance(value, _ALLOWED_VALUE_TYPES):
            raise ShadowValidationError(
                f"{field}.{key} 的值只允许字符串/数字/布尔/null，不支持嵌套对象或数组"
            )
        try:
            size = len(json.dumps(value, ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise ShadowValidationError(f"{field}.{key} 的值无法序列化为 JSON") from exc
        if size > MAX_VALUE_JSON_LEN:
            raise ShadowValidationError(
                f"{field}.{key} 的值序列化后超过 {MAX_VALUE_JSON_LEN} 字节"
            )


def _apply_patch(current: Dict[str, Any], patch: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """对 current 应用 merge patch；返回 (新 dict, 是否真的有变化)。

    规则：
    - value 为 None 表示删除该键；
    - 与现有值相同则不视为变化；
    - 不修改传入的 current，始终返回新 dict，避免 SQLAlchemy JSON 变更追踪失效。
    """
    new_state = dict(current or {})
    changed = False
    for key, value in patch.items():
        if value is None:
            if key in new_state:
                del new_state[key]
                changed = True
            continue
        if key not in new_state or new_state[key] != value:
            new_state[key] = value
            changed = True
    return new_state, changed


def compute_diff(desired: Dict[str, Any], reported: Dict[str, Any]) -> List[str]:
    """desired 与 reported 不一致的键：reported 缺少或值不同都计入。"""
    keys = set(desired or {})
    diff: List[str] = []
    for key in keys:
        if key not in (reported or {}):
            diff.append(key)
            continue
        if reported[key] != desired[key]:
            diff.append(key)
    return sorted(diff)


def compute_sync_status(
    desired: Dict[str, Any],
    reported: Dict[str, Any],
    device_status: str,
) -> str:
    """同步状态机：

    - empty：当前没有期望（desired 为空 dict，包括期望被全部清除）；
    - synced：desired 非空且所有期望键都与 reported 一致；
    - pending：在线且存在 diff；
    - offline_pending：离线且存在 diff（设备上线后需主动拉取）。
    """
    diff = compute_diff(desired, reported)
    if not desired and not diff:
        return "empty"
    if not diff:
        return "synced"
    return "offline_pending" if device_status == "offline" else "pending"


def _changed_keys(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """返回前后快照中新增、删除或值发生变化的键。"""
    return sorted(
        key
        for key in set(before or {}) | set(after or {})
        if key not in before or key not in after or before[key] != after[key]
    )


async def _record_revision(
    session: AsyncSession,
    row: DeviceShadow,
    *,
    action: str,
    operator: Optional[str],
    changed_keys: Optional[Sequence[str]] = None,
    source_version: Optional[int] = None,
    created_at: Optional[datetime] = None,
) -> DeviceShadowRevision:
    revision = DeviceShadowRevision(
        device_id=row.device_id,
        version=int(row.version or 0),
        desired=dict(row.desired or {}),
        changed_keys=sorted(set(changed_keys or [])),
        action=action,
        source_version=source_version,
        created_by=operator,
    )
    if created_at is not None:
        revision.created_at = created_at
    session.add(revision)
    await session.flush()
    return revision


async def _ensure_current_revision(
    session: AsyncSession, row: DeviceShadow, operator: str
) -> None:
    """为未经过 v1.37 迁移的当前状态补基线，保证首次变更也可回退。"""
    stmt = select(DeviceShadowRevision.id).where(
        DeviceShadowRevision.device_id == row.device_id,
        DeviceShadowRevision.version == int(row.version or 0),
    )
    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        return
    await _record_revision(
        session,
        row,
        action="baseline",
        operator=row.desired_updated_by or operator,
        created_at=row.desired_updated_at or row.created_at or _now(),
    )


async def _ensure_row(session: AsyncSession, device_id: str) -> DeviceShadow:
    row = await session.get(DeviceShadow, device_id)
    if row is None:
        row = DeviceShadow(device_id=device_id, desired={}, reported={}, version=0)
        session.add(row)
        await session.flush()
        await _record_revision(session, row, action="baseline", operator=None)
    return row


async def get_shadow(
    session: AsyncSession, device_id: str, *, create_if_missing: bool = True
) -> Optional[DeviceShadow]:
    if create_if_missing:
        return await _ensure_row(session, device_id)
    return await session.get(DeviceShadow, device_id)


def build_out(row: DeviceShadow, device_status: str) -> Dict[str, Any]:
    """构造路由层返回结构（与 DeviceShadowOut 字段一一对应）。"""
    desired = dict(row.desired or {})
    reported = dict(row.reported or {})
    diff = compute_diff(desired, reported)
    return {
        "device_id": row.device_id,
        "desired": desired,
        "reported": reported,
        "version": int(row.version or 0),
        "reported_version": int(row.reported_version or 0),
        "sync_status": compute_sync_status(desired, reported, device_status),
        "diff_keys": diff,
        "pending_desired": {key: desired[key] for key in diff},
        "desired_updated_at": row.desired_updated_at,
        "desired_updated_by": row.desired_updated_by,
        "reported_updated_at": row.reported_updated_at,
    }


async def apply_desired_patch(
    session: AsyncSession,
    device_id: str,
    patch: Dict[str, Any],
    operator: str,
    *,
    action: str = "merge",
) -> Tuple[DeviceShadow, bool]:
    """合并写入期望状态；返回 (row, 是否真的变更)。

    纯幂等 patch（所有键已处于目标值）不递增版本号，也不新增历史快照。
    """
    _validate_patch(patch, field="patch")
    row = await _ensure_row(session, device_id)
    await _ensure_current_revision(session, row, operator)
    before = dict(row.desired or {})
    new_desired, changed = _apply_patch(before, patch)
    if changed:
        row.desired = new_desired
        row.version = int(row.version or 0) + 1
        row.desired_updated_at = _now()
        row.desired_updated_by = operator
        await _record_revision(
            session,
            row,
            action=action,
            operator=operator,
            changed_keys=_changed_keys(before, new_desired),
        )
    await session.flush()
    return row, changed


async def apply_reported(
    session: AsyncSession,
    device_id: str,
    reported: Dict[str, Any],
    applied_version: int,
) -> DeviceShadow:
    """设备端上报实际状态：merge 语义；同时推进 reported_version。

    - reported_version 只前进不后退：忽略乱序到达的旧版本回执；
    - applied_version 大于当前 desired 版本时按版本夹紧（避免脏数据让同步状态异常）。
    """
    _validate_patch(reported, field="reported")
    row = await _ensure_row(session, device_id)
    new_reported, _ = _apply_patch(row.reported or {}, reported)
    row.reported = new_reported
    if applied_version > int(row.reported_version or 0):
        # 夹紧到当前 desired 版本，避免设备伪造一个超前版本号导致同步状态失真
        row.reported_version = min(applied_version, int(row.version or 0))
    row.reported_updated_at = _now()
    await session.flush()
    return row


async def clear_desired_keys(
    session: AsyncSession,
    device_id: str,
    keys: Sequence[str],
    operator: str,
) -> Tuple[DeviceShadow, List[str]]:
    """清除指定键的期望；返回 (row, 实际被清除的键)。"""
    row = await _ensure_row(session, device_id)
    await _ensure_current_revision(session, row, operator)
    before = dict(row.desired or {})
    patch = {key: None for key in keys if key}
    new_desired, _ = _apply_patch(before, patch)
    removed = sorted(set(before.keys()) - set(new_desired.keys()))
    if removed:
        row.desired = new_desired
        row.version = int(row.version or 0) + 1
        row.desired_updated_at = _now()
        row.desired_updated_by = operator
        await _record_revision(
            session,
            row,
            action="clear",
            operator=operator,
            changed_keys=removed,
        )
    await session.flush()
    return row, removed


async def resolve_target_devices(
    session: AsyncSession,
    device_ids: Optional[Sequence[str]],
    tags: Optional[Sequence[str]],
    group: Optional[str],
) -> Tuple[List[str], List[str]]:
    """解析批量下发的目标设备集合；返回 (existing_ids, missing_ids)。

    规则：
    - 显式 device_ids：直接校验存在性，返回 (存在的, 不存在的)；
    - tags / group 筛选：只返回存在的设备，missing 永远为空；
    - 两者同时提供：取交集（device_ids ∩ 筛选结果）。
    """
    from app.services import tag_service  # 延迟导入避免循环

    if device_ids:
        stmt = select(Device.device_id).where(Device.device_id.in_(list(device_ids)))
        requested_existing = {str(d) for d in (await session.execute(stmt)).scalars().all()}
        missing = [d for d in device_ids if d not in requested_existing]
        existing = requested_existing
        if tags or group:
            filter_stmt = select(Device.device_id)
            if group:
                filter_stmt = filter_stmt.where(Device.group_name == group)
            if tags:
                subq = tag_service.device_ids_with_tags(tags)
                if subq is not None:
                    filter_stmt = filter_stmt.where(Device.device_id.in_(subq))
            matched = {str(d) for d in (await session.execute(filter_stmt)).scalars().all()}
            existing = existing & matched
        return sorted(existing), missing

    # 仅筛选模式
    filter_stmt = select(Device.device_id)
    if group:
        filter_stmt = filter_stmt.where(Device.group_name == group)
    if tags:
        subq = tag_service.device_ids_with_tags(tags)
        if subq is not None:
            filter_stmt = filter_stmt.where(Device.device_id.in_(subq))
    matched = sorted({str(d) for d in (await session.execute(filter_stmt)).scalars().all()})
    return matched, []


async def batch_apply_desired(
    session: AsyncSession,
    device_ids: Sequence[str],
    patch: Dict[str, Any],
    operator: str,
) -> List[Dict[str, Any]]:
    """对一组设备应用同一个 patch；逐设备返回真实状态。"""
    results: List[Dict[str, Any]] = []
    for device_id in device_ids:
        row, changed = await apply_desired_patch(
            session, device_id, patch, operator, action="batch"
        )
        results.append(
            {
                "device_id": device_id,
                "status": "updated" if changed else "unchanged",
                "version": int(row.version or 0),
                "error": None,
            }
        )
    return results


async def list_revisions(
    session: AsyncSession,
    device_id: str,
    *,
    limit: int,
    offset: int,
) -> Tuple[List[DeviceShadowRevision], int]:
    """按版本倒序返回不可变快照；offset/limit 只影响查询，不改变历史。"""
    filters = (DeviceShadowRevision.device_id == device_id,)
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(DeviceShadowRevision).where(*filters)
            )
        ).scalar_one()
    )
    stmt = (
        select(DeviceShadowRevision)
        .where(*filters)
        .order_by(DeviceShadowRevision.version.desc())
        .offset(offset)
        .limit(limit)
    )
    items = list((await session.execute(stmt)).scalars().all())
    return items, total


async def rollback_desired(
    session: AsyncSession,
    device_id: str,
    target_version: int,
    operator: str,
) -> Tuple[DeviceShadow, bool, List[str]]:
    """用目标版本的完整快照替换当前期望，并生成一个新版本。

    这是精确替换而不是 merge：目标快照没有的当前键会被移除。reported 与
    reported_version 保持不动，由设备下一次主动同步确认新期望。若当前期望已经
    与目标快照一致，则幂等返回，不空增版本。
    """
    row = await _ensure_row(session, device_id)
    await _ensure_current_revision(session, row, operator)
    stmt = select(DeviceShadowRevision).where(
        DeviceShadowRevision.device_id == device_id,
        DeviceShadowRevision.version == target_version,
    )
    target = (await session.execute(stmt)).scalar_one_or_none()
    if target is None:
        raise ShadowRevisionNotFound(f"设备 {device_id} 的影子版本 v{target_version} 不存在")

    before = dict(row.desired or {})
    restored = dict(target.desired or {})
    changed_keys = _changed_keys(before, restored)
    if not changed_keys:
        return row, False, []

    row.desired = restored
    row.version = int(row.version or 0) + 1
    row.desired_updated_at = _now()
    row.desired_updated_by = operator
    await _record_revision(
        session,
        row,
        action="rollback",
        operator=operator,
        changed_keys=changed_keys,
        source_version=target_version,
    )
    await session.flush()
    return row, True, changed_keys


async def delete_device_shadow(session: AsyncSession, device_id: str) -> None:
    """设备删除时同时清理当前影子与版本历史，避免孤儿行。"""
    await session.execute(
        delete(DeviceShadowRevision).where(DeviceShadowRevision.device_id == device_id)
    )
    await session.execute(delete(DeviceShadow).where(DeviceShadow.device_id == device_id))
