"""告警规则引擎：可配置阈值的确定性判定（v1.34.0）。

与 v1.33.0 的差异：
- 阈值不再只存在于 Settings / rule_config，而是 `alert_rule` 表中的一条条规则
  （指标、比较方向、阈值、级别、生效范围、启用状态、连续次数、优先级）；
- 规则由 alert_rule_service 全量加载进进程内缓存 `_RULES`，每次上报实时读缓存，
  管理端增删改后用 write-through 刷新，**无需重启**即可生效；
- 历史默认阈值以 `is_default=True` 的 10 条规则落库（迁移/首次启动补齐），
  连续次数为 1、范围为 global，因此同一份数据在升级前后的判定结果完全一致。

裁决口径（docs/contracts.md 3.4）：
- 同一指标多条规则同时命中 → **取最高级别**（critical 胜 warning，与 v1.33.0 的
  「critical 命中后不再叠加 warning」语义一致），同级按 priority 升序、再按 id 升序；
- 每个指标最终只产一条告警，保持既有抑制/看板/移动端「一条告警一个指标」的语义。
"""

from typing import Dict, List, Optional, Sequence, Tuple

from app import rule_conditions
from app.config import get_settings

METRICS = ("voltage", "temperature", "humidity", "rssi")
LEVELS = ("warning", "critical")
LEVEL_RANK = {"warning": 1, "critical": 2}
# 指标物理方向：反向比较（如「电压高于 3.4V 告警」）属于误配，写入时拒绝
METRIC_OPS = {
    "voltage": ("lt",),
    "temperature": ("lt", "gt"),
    "humidity": ("gt",),
    "rssi": ("lt",),
}

# v1.33.0 环境变量/覆盖表的 10 个阈值键 → 默认规则定义。
# config_key 同时是 rule_config 的主键，升级时据此把在线修改过的阈值带入默认规则。
DEFAULT_RULE_SPECS: Tuple[Dict, ...] = (
    {
        "config_key": "threshold_voltage_critical",
        "name": "电压过低-严重",
        "metric": "voltage",
        "op": "lt",
        "level": "critical",
        "priority": 90,
    },
    {
        "config_key": "threshold_voltage_warning",
        "name": "电压过低-警告",
        "metric": "voltage",
        "op": "lt",
        "level": "warning",
        "priority": 100,
    },
    {
        "config_key": "threshold_temperature_critical_high",
        "name": "温度过高-严重",
        "metric": "temperature",
        "op": "gt",
        "level": "critical",
        "priority": 90,
    },
    {
        "config_key": "threshold_temperature_warning_high",
        "name": "温度过高-警告",
        "metric": "temperature",
        "op": "gt",
        "level": "warning",
        "priority": 100,
    },
    {
        "config_key": "threshold_temperature_critical_low",
        "name": "温度过低-严重",
        "metric": "temperature",
        "op": "lt",
        "level": "critical",
        "priority": 90,
    },
    {
        "config_key": "threshold_temperature_warning_low",
        "name": "温度过低-警告",
        "metric": "temperature",
        "op": "lt",
        "level": "warning",
        "priority": 100,
    },
    {
        "config_key": "threshold_humidity_critical",
        "name": "湿度偏高-严重",
        "metric": "humidity",
        "op": "gt",
        "level": "critical",
        "priority": 90,
    },
    {
        "config_key": "threshold_humidity_warning",
        "name": "湿度偏高-警告",
        "metric": "humidity",
        "op": "gt",
        "level": "warning",
        "priority": 100,
    },
    {
        "config_key": "threshold_rssi_critical",
        "name": "信号过弱-严重",
        "metric": "rssi",
        "op": "lt",
        "level": "critical",
        "priority": 90,
    },
    {
        "config_key": "threshold_rssi_warning",
        "name": "信号过弱-警告",
        "metric": "rssi",
        "op": "lt",
        "level": "warning",
        "priority": 100,
    },
)

# config_key → thresholds_snapshot 的旧结构位置（兼容 /api/rules/thresholds）
CONFIG_KEY_TO_SNAPSHOT = {
    "threshold_voltage_warning": ("voltage", "warning"),
    "threshold_voltage_critical": ("voltage", "critical"),
    "threshold_temperature_warning_high": ("temperature", "warning_high"),
    "threshold_temperature_critical_high": ("temperature", "critical_high"),
    "threshold_temperature_warning_low": ("temperature", "warning_low"),
    "threshold_temperature_critical_low": ("temperature", "critical_low"),
    "threshold_humidity_warning": ("humidity", "warning"),
    "threshold_humidity_critical": ("humidity", "critical"),
    "threshold_rssi_warning": ("rssi", "warning"),
    "threshold_rssi_critical": ("rssi", "critical"),
}

# 进程内阈值覆盖缓存：{settings 属性名: 数值}，由 rule_service 维护（兼容旧接口）
_OVERRIDES: Dict[str, float] = {}

# 进程内规则缓存：由 alert_rule_service 在启动/写入后整体替换；None 表示尚未加载，
# 此时用 Settings + 覆盖合成默认规则，保证纯函数测试与旧调用方式仍可工作。
_RULES: Optional[List[Dict]] = None

# 连续越限计数：{(device_id, 规则标识): 连续次数}。
# 只做进程内计数：高频上报场景写入计数表代价过大；重启后清零，最多延迟 N-1 次上报。
_CONSECUTIVE: Dict[Tuple[str, str], int] = {}


def set_overrides(overrides: Dict[str, float]) -> None:
    """整体替换覆盖缓存（启动时从 rule_config 表加载）。"""
    _OVERRIDES.clear()
    _OVERRIDES.update(overrides)


def _sync_rule_threshold(config_key: str, value: Optional[float]) -> None:
    """把覆盖值同步到已加载的默认规则，保证旧 PUT 接口热生效。"""
    if _RULES is None:
        return
    fallback = getattr(get_settings(), config_key)
    resolved = float(value) if value is not None else float(fallback)
    for rule in _RULES:
        if rule.get("is_default") and rule.get("config_key") == config_key:
            rule["threshold"] = resolved


def patch_override(key: str, value: float) -> None:
    """更新单个覆盖项（PUT 接口写入后调用）。"""
    _OVERRIDES[key] = value
    _sync_rule_threshold(key, value)


def clear_override(key: str) -> None:
    _OVERRIDES.pop(key, None)
    _sync_rule_threshold(key, None)


def overrides_snapshot() -> Dict[str, float]:
    return dict(_OVERRIDES)


def default_threshold(config_key: str) -> float:
    """默认规则阈值：覆盖缓存优先，其次环境变量配置。"""
    if config_key in _OVERRIDES:
        return float(_OVERRIDES[config_key])
    return float(getattr(get_settings(), config_key))


def default_rules() -> List[Dict]:
    """按 Settings + 覆盖合成 10 条默认规则（未从数据库加载时的兜底视图）。"""
    rules: List[Dict] = []
    for index, spec in enumerate(DEFAULT_RULE_SPECS, start=1):
        rules.append(
            {
                "id": None,
                "name": spec["name"],
                "metric": spec["metric"],
                "op": spec["op"],
                "threshold": default_threshold(spec["config_key"]),
                "level": spec["level"],
                "scope_type": "global",
                "scope_value": None,
                "enabled": True,
                "consecutive_count": 1,
                "priority": spec["priority"],
                "is_default": True,
                "config_key": spec["config_key"],
                "_order": index,
            }
        )
    return rules


def reset_rules_cache() -> None:
    """清空规则缓存（进程重启或测试隔离）。"""
    global _RULES
    _RULES = None
    _CONSECUTIVE.clear()


def set_rules(rows: Optional[Sequence[Dict]]) -> None:
    """整体替换规则缓存（None 表示回退到 Settings 合成视图）。"""
    global _RULES
    if rows is None:
        _RULES = None
        _CONSECUTIVE.clear()
        return
    _RULES = [dict(row) for row in rows]


def rules_snapshot() -> List[Dict]:
    """当前生效规则（只读副本），供内部排查与测试。"""
    return [dict(rule) for rule in (_RULES if _RULES is not None else default_rules())]


def reset_consecutive() -> None:
    """清空连续计数（进程重启或测试隔离）。"""
    _CONSECUTIVE.clear()


def _active_rules() -> List[Dict]:
    return _RULES if _RULES is not None else default_rules()


def _snapshot_rule(config_key: str) -> Optional[Dict]:
    for rule in _active_rules():
        if rule.get("is_default") and rule.get("config_key") == config_key:
            return rule
    return None


def _thresholds() -> Dict:
    """组装当前生效阈值（兼容 v1.33.0 的嵌套结构）。"""
    result: Dict[str, Dict[str, Tuple[float, str]]] = {}
    for config_key, (group, name) in CONFIG_KEY_TO_SNAPSHOT.items():
        rule = _snapshot_rule(config_key)
        if rule is not None:
            value, op = float(rule["threshold"]), rule["op"]
        else:
            value = default_threshold(config_key)
            op = next(spec["op"] for spec in DEFAULT_RULE_SPECS if spec["config_key"] == config_key)
        result.setdefault(group, {})[name] = (value, op)
    return result


def thresholds_snapshot() -> Dict:
    """对外暴露当前生效阈值，供 /api/rules/thresholds 接口返回。"""
    return _thresholds()


def _rule_identity(rule: Dict) -> str:
    if rule.get("id") is None:
        return f"{rule['name']}:{rule['op']}:{rule['level']}"
    return f"id:{rule['id']}"


def _rule_scope_values(rule: Dict) -> List[str]:
    """规则多值范围；v1.34.0 单值规则回退到 scope_value。"""
    values = list(rule.get("scope_values") or [])
    if not values and rule.get("scope_value"):
        values = [rule["scope_value"]]
    return values


def _in_scope(
    rule: Dict,
    device_id: Optional[str],
    group_name: Optional[str],
    tag_names: Optional[Sequence[str]] = None,
) -> bool:
    """范围匹配：条件树优先；无条件树时沿用 v1.35.0 单维度多值「或」。

    v1.36.0 起规则可带 `conditions`（all/any 嵌套）。迁移会把老规则等价转换，
    因此老规则升级后走条件树分支；`conditions` 为空（NULL）时保持老字段语义，
    保证 downgrade 与「自定义条件树被清空」的场景不会误命中。
    """
    conditions = rule.get("conditions")
    if conditions:
        return rule_conditions.matches(
            conditions, device_id=device_id, group_name=group_name, tag_names=tag_names
        )
    scope_type = rule.get("scope_type") or "global"
    if scope_type == "global":
        return True
    values = _rule_scope_values(rule)
    if not values:
        return False
    if scope_type == "group":
        return bool(group_name) and group_name in values
    if scope_type == "device":
        return bool(device_id) and device_id in values
    if scope_type == "tag":
        return bool(tag_names) and any(tag in values for tag in tag_names)
    # custom 但没有条件树（被清空/回滚中间态）：fail-closed，不当成全局
    return False


def _metric_value(report, metric: str) -> Optional[float]:
    raw = getattr(report, metric, None)
    if raw is None:
        return None
    return float(raw)


def _triggered(rule: Dict, value: float) -> bool:
    # 严格小于/大于：等于阈值不告警，与 v1.33.0 一致
    if rule["op"] == "lt":
        return value < float(rule["threshold"])
    return value > float(rule["threshold"])


def _preferred(current: Optional[Dict], candidate: Dict) -> Dict:
    """同指标裁决：级别高者优先；同级按 priority 升序、再按 id 升序。"""
    if current is None:
        return candidate
    if LEVEL_RANK[candidate["level"]] != LEVEL_RANK[current["level"]]:
        return candidate if LEVEL_RANK[candidate["level"]] > LEVEL_RANK[current["level"]] else current

    def order(hit: Dict) -> Tuple[int, int, int]:
        return (
            hit["priority"],
            hit["rule_id"] if hit["rule_id"] is not None else 10**9,
            hit.get("_order") or 0,
        )

    return candidate if order(candidate) < order(current) else current


def evaluate(
    report,
    device_id: Optional[str] = None,
    group_name: Optional[str] = None,
    tag_names: Optional[Sequence[str]] = None,
) -> List[Dict]:
    """对一条上报执行规则判定，返回命中的告警列表（每指标最多一条）。

    参数：
    - `report`：含 voltage/temperature/humidity/rssi 字段的上报对象；
    - `device_id` / `group_name` / `tag_names`：用于「指定设备 / 分组 / 标签」
      范围匹配；缺省时仅 global 规则生效（纯函数测试无需构造设备）。
    - 规则内多个范围值为「或」；同一指标多条规则命中仍取最高级别。
    """
    device = device_id or getattr(report, "device_id", None)
    hits: Dict[str, Dict] = {}

    for rule in _active_rules():
        if not rule.get("enabled", True):
            continue
        metric = rule.get("metric")
        value = _metric_value(report, metric) if metric in METRICS else None
        if value is None:
            continue

        rule_key = _rule_identity(rule)
        counter_key = (device or "", rule_key)
        scoped = _in_scope(rule, device, group_name, tag_names)
        triggered = scoped and _triggered(rule, value)

        if not triggered:
            _CONSECUTIVE.pop(counter_key, None)
            continue

        count = _CONSECUTIVE.get(counter_key, 0) + 1
        _CONSECUTIVE[counter_key] = count
        if count < int(rule.get("consecutive_count") or 1):
            continue

        hit = {
            "metric": metric,
            "level": rule["level"],
            "value": value,
            "threshold": float(rule["threshold"]),
            "rule_id": rule.get("id"),
            "rule_name": rule["name"],
            "op": rule["op"],
            "consecutive_count": int(rule.get("consecutive_count") or 1),
            "priority": rule.get("priority", 100),
            "_order": rule.get("_order") or rule.get("id") or 0,
        }
        hits[metric] = _preferred(hits.get(metric), hit)

    return [
        {
            "metric": hit["metric"],
            "level": hit["level"],
            "value": hit["value"],
            "threshold": hit["threshold"],
            "rule_id": hit["rule_id"],
            "rule_name": hit["rule_name"],
            "op": hit["op"],
            "consecutive_count": hit["consecutive_count"],
        }
        for hit in hits.values()
    ]
