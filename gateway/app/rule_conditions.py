"""告警规则条件树（v1.36.0）。

结构（JSON，存 `alert_rule.conditions`）：

```
{"op": "all", "children": [
   {"dim": "tag",   "values": ["冷库"]},
   {"dim": "group", "values": ["华东"]}
]}
```

- 节点：`{"op": "all" | "any", "children": [节点或叶子, ...]}`；
  `all` = 且，`any` = 或；两种节点可任意嵌套。
- 叶子：`{"dim": "group" | "device" | "tag", "values": [...]}`；
  一个叶子内部多个值为「或」（与 v1.35.0 语义一致）。
- 空条件树非法：**全局规则用 `scope_type=global` 且 `conditions IS NULL` 表达**，
  不允许用空 `children` 表示「命中一切」，避免隐式放行。

限制与理由：
- 深度 ≤3（含最外层）：嵌套两层的表达能力覆盖「(标签A 或 标签B) 且 分组C」，
  再深只会让界面与排障难以解释；同时限制递归校验/求值的开销。
- 叶子 ≤20：与单规则范围值总量同量级，保证求值恒定廉价。
- 单叶子值 ≤50：沿用 v1.35.0 的上限。

老字段映射（兼容 v1.35 与 downgrade）：
- 单叶子根节点（`any` + 1 个叶子）会镜像回 `scope_type/scope_value/scope_values`，
  因此 v1.35 与旧客户端仍能读到等价范围；
- 真正嵌套的条件树无法用单值字段表达，镜像为 `scope_type=custom`、老字段清空；
  回滚到 v1.35 后这类规则不命中（fail-closed，不会误报）。
"""

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services import tag_service

DIMENSIONS = ("group", "device", "tag")
OPS = ("all", "any")
MAX_DEPTH = 3
MAX_LEAVES = 20
MAX_VALUES_PER_LEAF = 50
MAX_VALUE_LENGTH = 64

DIM_LABEL = {"group": "分组", "device": "设备", "tag": "标签"}


class ConditionError(ValueError):
    """条件树非法：服务层转成可读的 400。"""


def _normalize_leaf(raw: Dict[str, Any]) -> Dict[str, Any]:
    extra = set(raw) - {"dim", "values"}
    if extra:
        raise ConditionError(f"条件叶子包含未知字段：{'、'.join(sorted(extra))}（只允许 dim/values）")
    dim = raw.get("dim")
    if dim not in DIMENSIONS:
        raise ConditionError(
            f"未知的匹配维度：{dim!r}（只支持 group / device / tag）"
        )
    values = raw.get("values")
    if not isinstance(values, list):
        raise ConditionError("条件叶子的 values 必须是数组")
    if not values:
        raise ConditionError(f"{DIM_LABEL[dim]}条件至少需要一个值")
    if len(values) > MAX_VALUES_PER_LEAF:
        raise ConditionError(f"单个条件最多 {MAX_VALUES_PER_LEAF} 个值，当前 {len(values)} 个")

    cleaned: List[str] = []
    seen = set()
    for item in values:
        if not isinstance(item, str):
            raise ConditionError("条件值必须是字符串")
        text = item.strip()
        if not text:
            raise ConditionError("条件值不能为空或纯空白")
        if dim == "tag":
            try:
                text = tag_service.normalize_tag(text)
            except tag_service.TagValidationError as exc:
                raise ConditionError(f"标签不合法：{exc}") from exc
        elif len(text) > MAX_VALUE_LENGTH:
            raise ConditionError(f"单个条件值最多 {MAX_VALUE_LENGTH} 个字符")
        if text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return {"dim": dim, "values": cleaned}


def _normalize_node(raw: Dict[str, Any], depth: int, counter: List[int]) -> Dict[str, Any]:
    extra = set(raw) - {"op", "children"}
    if extra:
        raise ConditionError(f"条件组包含未知字段：{'、'.join(sorted(extra))}（只允许 op/children）")
    op = raw.get("op")
    if op not in OPS:
        raise ConditionError("条件组合只能是 all（且）或 any（或）")
    children = raw.get("children")
    if not isinstance(children, list):
        raise ConditionError("条件组的 children 必须是数组")
    if not children:
        raise ConditionError("条件组至少需要一个子条件；全局范围请选择「全局」而不是空条件")
    if depth > MAX_DEPTH:
        raise ConditionError(f"条件树最多嵌套 {MAX_DEPTH} 层（含最外层），当前超过限制")

    normalized: List[Dict[str, Any]] = []
    for child in children:
        if not isinstance(child, dict):
            raise ConditionError("子条件必须是对象")
        if "op" in child:
            normalized.append(_normalize_node(child, depth + 1, counter))
        elif "dim" in child:
            counter[0] += 1
            if counter[0] > MAX_LEAVES:
                raise ConditionError(f"单条规则最多 {MAX_LEAVES} 个条件，当前超过限制")
            normalized.append(_normalize_leaf(child))
        else:
            raise ConditionError("子条件必须包含 op（条件组）或 dim（叶子）")
    return {"op": op, "children": normalized}


def normalize(raw: Any) -> Dict[str, Any]:
    """校验并规范化条件树；非法时抛 `ConditionError`（中文可读原因）。"""
    if not isinstance(raw, dict):
        raise ConditionError("条件树必须是对象")
    if "op" not in raw:
        raise ConditionError("条件树最外层必须是条件组（含 op 与 children）")
    return _normalize_node(raw, 1, [0])


def signature(conditions: Optional[Dict[str, Any]]) -> str:
    """稳定的比较用签名（键排序、紧凑分隔），用于判断两棵条件树是否等价。"""
    return json.dumps(conditions or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _leaf_matches(leaf: Dict[str, Any], *, device_id, group_name, tag_names) -> bool:
    values = leaf.get("values") or []
    dim = leaf.get("dim")
    if dim == "group":
        return bool(group_name) and group_name in values
    if dim == "device":
        return bool(device_id) and device_id in values
    if dim == "tag":
        return bool(tag_names) and any(tag in values for tag in tag_names)
    return False


def matches(
    conditions: Optional[Dict[str, Any]],
    *,
    device_id: Optional[str],
    group_name: Optional[str],
    tag_names: Optional[Sequence[str]] = None,
) -> bool:
    """条件树求值：组内 `all`=且、`any`=或；叶子内部多值=或。

    条件树缺失或结构异常时返回 False（fail-closed），不把未知结构当成全局。
    """
    if not conditions:
        return False

    def walk(node: Dict[str, Any]) -> bool:
        if "op" in node:
            children = node.get("children") or []
            if not children:
                # 空条件组非法（校验阶段已拒绝）；这里 fail-closed，
                # 避免 `all([])` 被当成「命中一切」的隐性全局。
                return False
            results = [walk(child) for child in children]
            return all(results) if node.get("op") == "all" else any(results)
        return _leaf_matches(node, device_id=device_id, group_name=group_name, tag_names=tag_names)

    try:
        return walk(conditions)
    except (TypeError, AttributeError, KeyError):
        return False


def legacy_mirror(
    conditions: Dict[str, Any],
) -> Tuple[str, Optional[str], Optional[List[str]]]:
    """把单叶子条件树镜像回 v1.35 的老字段；嵌套树返回 `custom` + 空老字段。

    只有一个叶子时 `all` 与 `any` 语义相同，两者都按叶子维度镜像，
    使「单条件规则」在 downgrade 后仍能被 v1.35 正确读取。
    """
    if conditions.get("op") in OPS:
        children = conditions.get("children") or []
        if len(children) == 1 and "dim" in children[0]:
            leaf = children[0]
            values = list(leaf.get("values") or [])
            if values:
                return leaf["dim"], values[0], values
    return "custom", None, None
