"""设备标签服务（v1.35.0）。

标签与分组（`device.group_name`）的区别：
- 分组是设备的**单值**属性（一台设备只在一个分组），用于归属与批量运维；
- 标签是**多值**维度（一台设备可同时属于「华东」「产线A」「关键设备」），
  存于独立的 `device_tag` 关联表，可被设备列表、告警列表、导出与告警规则共用。

权限取舍：写标签只允许 `admin`。标签会直接决定告警规则命中范围与导出范围，
属于跨团队口径变更；`operator` 仍可只读查看，`viewer` 只读。
"""

import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceTag

# 标签长度与单设备数量上限：过长的标签无法在规则范围里紧凑展示，
# 单设备标签过多会让「为什么命中」的解释变得不可读，因此显式设上限。
TAG_MAX_LENGTH = 32
TAG_MAX_PER_DEVICE = 16
TAG_MAX_PER_REQUEST = 50
# 允许：字母、数字、中文、下划线、短横线；不允许空白与其它符号（避免逗号拼接歧义）
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]+$")


class TagValidationError(ValueError):
    """标签格式或数量不合法；路由层转换为 400 + 可读文案。"""


def normalize_tag(raw: Optional[str]) -> str:
    text = (raw or "").strip()
    if not text:
        raise TagValidationError("标签不能为空或纯空白")
    if len(text) > TAG_MAX_LENGTH:
        raise TagValidationError(f"标签最多 {TAG_MAX_LENGTH} 个字符（当前 {len(text)}）")
    if not _TAG_PATTERN.fullmatch(text):
        raise TagValidationError(
            "标签只允许字母、数字、中文、下划线和短横线，不能包含空格或其它符号"
        )
    return text


def normalize_tags(raw: Optional[Iterable[str]]) -> List[str]:
    """去空、去重、排序并逐项校验；超过单次上限直接拒绝。"""
    values: List[str] = []
    seen = set()
    for item in raw or []:
        tag = normalize_tag(item)
        if tag in seen:
            continue
        seen.add(tag)
        values.append(tag)
    if len(values) > TAG_MAX_PER_REQUEST:
        raise TagValidationError(f"单次最多操作 {TAG_MAX_PER_REQUEST} 个标签")
    return sorted(values)


async def list_tags(session: AsyncSession) -> List[Dict]:
    """标签列表与设备数（按设备数倒序、标签名升序）。"""
    stmt = (
        select(DeviceTag.tag, func.count(DeviceTag.device_id))
        .group_by(DeviceTag.tag)
        .order_by(func.count(DeviceTag.device_id).desc(), DeviceTag.tag)
    )
    rows = (await session.execute(stmt)).all()
    return [{"tag": str(tag), "device_count": int(count)} for tag, count in rows]


async def device_tags(session: AsyncSession, device_id: str) -> List[str]:
    stmt = select(DeviceTag.tag).where(DeviceTag.device_id == device_id).order_by(DeviceTag.tag)
    return [str(tag) for tag in (await session.execute(stmt)).scalars().all()]


async def tags_for_devices(
    session: AsyncSession, device_ids: Sequence[str]
) -> Dict[str, List[str]]:
    """批量取标签，避免设备列表 N+1。"""
    if not device_ids:
        return {}
    stmt = (
        select(DeviceTag.device_id, DeviceTag.tag)
        .where(DeviceTag.device_id.in_(list(device_ids)))
        .order_by(DeviceTag.device_id, DeviceTag.tag)
    )
    result: Dict[str, List[str]] = {device_id: [] for device_id in device_ids}
    for device_id, tag in (await session.execute(stmt)).all():
        result.setdefault(str(device_id), []).append(str(tag))
    return result


async def _existing_device_ids(session: AsyncSession, device_ids: Sequence[str]) -> set:
    if not device_ids:
        return set()
    stmt = select(Device.device_id).where(Device.device_id.in_(list(device_ids)))
    return {str(row) for row in (await session.execute(stmt)).scalars().all()}


async def batch_update(
    session: AsyncSession,
    device_ids: Sequence[str],
    add_tags: Optional[Sequence[str]],
    remove_tags: Optional[Sequence[str]],
    operator: str,
) -> Dict:
    """批量打标签/移除标签，逐设备返回真实结果（不静默成功）。

    逐设备状态：
    - `not_found`：设备不存在，未做任何变更；
    - `over_limit`：变更后标签数会超过 16，整台设备跳过（不做部分写入）；
    - `updated`：至少新增或移除了一个标签；
    - `unchanged`：标签已处于目标状态（重复标签按幂等处理，不算失败）。
    """
    add = normalize_tags(add_tags)
    remove = normalize_tags(remove_tags)
    if not add and not remove:
        raise TagValidationError("请至少提供要添加或移除的标签")

    ids = list(dict.fromkeys(str(item) for item in device_ids if item))
    if not ids:
        raise TagValidationError("请选择至少一个设备")
    if len(ids) > 200:
        raise TagValidationError("单次最多操作 200 台设备")

    existing = await _existing_device_ids(session, ids)
    current_map = await tags_for_devices(session, [i for i in ids if i in existing])

    results: List[Dict] = []
    updated = unchanged = not_found = over_limit = 0
    for device_id in ids:
        if device_id not in existing:
            not_found += 1
            results.append(
                {"device_id": device_id, "status": "not_found", "added": [], "removed": [],
                 "tag_count": 0, "error": "设备不存在"}
            )
            continue

        current = set(current_map.get(device_id, []))
        to_add = [tag for tag in add if tag not in current]
        to_remove = [tag for tag in remove if tag in current]
        final_count = len(current) + len(to_add) - len(to_remove)
        if final_count > TAG_MAX_PER_DEVICE:
            over_limit += 1
            results.append(
                {"device_id": device_id, "status": "over_limit", "added": [], "removed": [],
                 "tag_count": len(current),
                 "error": f"标签数将达到 {final_count}，超过单设备上限 {TAG_MAX_PER_DEVICE}"}
            )
            continue

        if not to_add and not to_remove:
            unchanged += 1
            results.append(
                {"device_id": device_id, "status": "unchanged", "added": [], "removed": [],
                 "tag_count": len(current), "error": None}
            )
            continue

        for tag in to_add:
            session.add(DeviceTag(device_id=device_id, tag=tag, created_by=operator))
        if to_remove:
            await session.execute(
                delete(DeviceTag).where(
                    DeviceTag.device_id == device_id, DeviceTag.tag.in_(to_remove)
                )
            )
        updated += 1
        results.append(
            {"device_id": device_id, "status": "updated", "added": to_add, "removed": to_remove,
             "tag_count": final_count, "error": None}
        )

    await session.flush()
    return {
        "results": results,
        "updated": updated,
        "unchanged": unchanged,
        "not_found": not_found,
        "over_limit": over_limit,
        "added": len(add),
        "removed": len(remove),
    }


async def delete_device_tags(session: AsyncSession, device_id: str) -> None:
    """设备删除时清理其标签（标签无外键，显式清理避免孤儿行）。"""
    await session.execute(delete(DeviceTag).where(DeviceTag.device_id == device_id))


def device_ids_with_tags(tags: Optional[Sequence[str]]):
    """标签筛选子查询：多个标签之间为「或」（任一命中即包含该设备）。

    规则范围与列表筛选统一使用 OR 语义：标签是并列维度，
    要求「同时具备全部标签」会让筛选结果随标签增多而趋近于零，
    且用户无法从界面看出是交集还是并集。
    """
    if not tags:
        return None
    return select(DeviceTag.device_id).where(DeviceTag.tag.in_(list(tags))).distinct()


async def resolve_device_context(
    session: AsyncSession, device_id: str, group_name: Optional[str]
) -> Tuple[Optional[str], List[str]]:
    """规则判定所需的设备上下文（分组 + 标签）。"""
    return group_name, await device_tags(session, device_id)
