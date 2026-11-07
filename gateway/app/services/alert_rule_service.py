"""告警规则管理服务（v1.34.0）：CRUD、校验、默认规则补齐与缓存热刷新。

缓存策略：规则全量缓存在进程内（`rules.set_rules`），每次上报只读内存，
不在 MQTT 热路径上查库；管理端写入后 write-through 重载缓存，因此改规则
无需重启即可生效。代价是仅支持单实例网关（多实例部署时各进程需自行刷新，
本项目当前部署形态为单实例）。
"""

import decimal
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import rules
from app.config import get_settings
from app.models import AlertRule, RuleConfig
from app.schemas import AlertRuleCreate, AlertRuleUpdate
from app.services import tag_service

logger = logging.getLogger(__name__)

# 指标阈值合法区间：与上报数据的物理范围保持一致，越界一律拒绝
METRIC_RANGES = {
    "voltage": (2.0, 4.5, "电压应在 2.0~4.5 V 之间"),
    "temperature": (-40.0, 85.0, "温度应在 -40~85 ℃ 之间"),
    "humidity": (0.0, 100.0, "湿度应在 0~100 % 之间"),
    "rssi": (-150.0, 0.0, "信号强度应在 -150~0 dBm 之间"),
}

LEVEL_LABEL = {"warning": "警告", "critical": "严重"}

# (metric, op, level) → rule_config 主键：用于把默认规则的修改镜像回旧表，
# 保证 downgrade 回 v1.33.0 后仍保留最近一次阈值。
DEFAULT_CONFIG_KEY = {
    (spec["metric"], spec["op"], spec["level"]): spec["config_key"]
    for spec in rules.DEFAULT_RULE_SPECS
}


class RuleValidationError(ValueError):
    """规则校验失败：路由层转换成可读的 400。"""


def _config_key(rule: AlertRule) -> Optional[str]:
    if not rule.is_default:
        return None
    return DEFAULT_CONFIG_KEY.get((rule.metric, rule.op, rule.level))


def _scope_values_of(rule: AlertRule) -> List[str]:
    """读取规则多值范围；v1.34.0 单值行回退到 scope_value。"""
    values = list(rule.scope_values or [])
    if not values and rule.scope_value:
        values = [rule.scope_value]
    return values


def to_cache_dict(rule: AlertRule) -> Dict[str, Any]:
    scope_values = _scope_values_of(rule)
    return {
        "id": rule.id,
        "name": rule.name,
        "metric": rule.metric,
        "op": rule.op,
        "threshold": float(rule.threshold),
        "level": rule.level,
        "scope_type": rule.scope_type,
        "scope_value": rule.scope_value,
        "scope_values": scope_values,
        "enabled": bool(rule.enabled),
        "consecutive_count": int(rule.consecutive_count),
        "priority": int(rule.priority),
        "is_default": bool(rule.is_default),
        "config_key": _config_key(rule),
        "_order": rule.id,
    }


def to_out(rule: AlertRule) -> Dict[str, Any]:
    scope_values = _scope_values_of(rule)
    return {
        "id": rule.id,
        "name": rule.name,
        "metric": rule.metric,
        "op": rule.op,
        "threshold": float(rule.threshold),
        "level": rule.level,
        "scope_type": rule.scope_type,
        "scope_value": rule.scope_value,
        "scope_values": scope_values or None,
        "enabled": bool(rule.enabled),
        "consecutive_count": int(rule.consecutive_count),
        "priority": int(rule.priority),
        "is_default": bool(rule.is_default),
        "updated_by": rule.updated_by,
        "updated_at": rule.updated_at,
    }


async def load_rules(session: AsyncSession) -> int:
    """从 alert_rule 表全量加载规则到进程内缓存。"""
    rows = list((await session.execute(select(AlertRule).order_by(AlertRule.id))).scalars().all())
    rules.set_rules([to_cache_dict(row) for row in rows])
    logger.info("加载告警规则 %d 条", len(rows))
    return len(rows)


async def ensure_default_rules(session: AsyncSession) -> int:
    """表为空时补齐 10 条默认规则（迁移已种子的库不会重复写入）。

    阈值来源与 v1.33.0 完全一致：rule_config 在线覆盖 > Settings 环境变量。
    """
    total = int((await session.execute(select(func.count()).select_from(AlertRule))).scalar_one())
    if total > 0:
        return 0

    overrides = {
        row.key: float(row.value)
        for row in (await session.execute(select(RuleConfig))).scalars().all()
    }
    for spec in rules.DEFAULT_RULE_SPECS:
        key = spec["config_key"]
        value = overrides.get(key, rules.default_threshold(key))
        session.add(
            AlertRule(
                name=spec["name"],
                metric=spec["metric"],
                op=spec["op"],
                threshold=value,
                level=spec["level"],
                scope_type="global",
                scope_value=None,
                enabled=True,
                consecutive_count=1,
                priority=spec["priority"],
                is_default=True,
                updated_by="system",
            )
        )
    await session.flush()
    logger.info("空规则表已补齐 %d 条默认规则", len(rules.DEFAULT_RULE_SPECS))
    return len(rules.DEFAULT_RULE_SPECS)


async def list_rules(session: AsyncSession) -> List[AlertRule]:
    stmt = select(AlertRule).order_by(AlertRule.metric, AlertRule.priority, AlertRule.id)
    return list((await session.execute(stmt)).scalars().all())


def _validate_range(metric: str, threshold: float) -> None:
    low, high, message = METRIC_RANGES[metric]
    if threshold != threshold:  # NaN
        raise RuleValidationError("阈值必须是有效数字，不能是 NaN")
    if threshold in (float("inf"), float("-inf")):
        raise RuleValidationError("阈值必须是有效数字，不能是无穷大")
    if threshold < low or threshold > high:
        raise RuleValidationError(f"{message}，当前值 {threshold}")


def _validate_direction(metric: str, op: str) -> None:
    allowed = rules.METRIC_OPS[metric]
    if op not in allowed:
        direction = "低于阈值" if (allowed[0] == "lt" and len(allowed) == 1) else "高于阈值"
        raise RuleValidationError(
            f"指标 {metric} 的比较方向只能是「{direction}」，不能配置为 {'低于' if op == 'lt' else '高于'}阈值"
        )


async def _validate_consistency(
    session: AsyncSession,
    *,
    metric: str,
    op: str,
    level: str,
    scope_type: str,
    scope_value: Optional[str],
    threshold: float,
    exclude_id: Optional[int] = None,
) -> None:
    """同指标同方向同范围下，critical 必须比 warning 更严格。

    反向误配（例如「电压低于 3.6V 算严重、低于 3.4V 算警告」）会让低级别规则
    永远被高阈值覆盖，等于配置无效；这里在写入时直接拒绝，而不是运行时静默裁决。
    """
    counterpart = "warning" if level == "critical" else "critical"
    stmt = select(AlertRule).where(
        AlertRule.metric == metric,
        AlertRule.op == op,
        AlertRule.level == counterpart,
        AlertRule.scope_type == scope_type,
    )
    if scope_value is None:
        stmt = stmt.where(AlertRule.scope_value.is_(None))
    else:
        stmt = stmt.where(AlertRule.scope_value == scope_value)
    if exclude_id is not None:
        stmt = stmt.where(AlertRule.id != exclude_id)
    other = (await session.execute(stmt)).scalars().first()
    if other is None:
        return

    other_value = float(other.threshold)
    label = "低于" if op == "lt" else "高于"
    if level == "critical":
        stricter = threshold < other_value if op == "lt" else threshold > other_value
    else:
        stricter = threshold > other_value if op == "lt" else threshold < other_value
    if not stricter:
        # lt：critical 应更低、warning 应更高；gt 相反
        word = ("低于" if op == "lt" else "高于") if level == "critical" else ("高于" if op == "lt" else "低于")
        raise RuleValidationError(
            f"比较方向疑似反向：{LEVEL_LABEL[level]}规则阈值应{word}同范围的"
            f"{LEVEL_LABEL[counterpart]}阈值 {other_value}（{label} {other_value} 触发），"
            f"当前设置为 {threshold}；严重级别必须比警告级别更严格"
        )


async def _validate(
    session: AsyncSession,
    *,
    metric: str,
    op: str,
    level: str,
    threshold: float,
    scope_type: str,
    scope_value: Optional[str] = None,
    scope_values: Optional[List[str]] = None,
    exclude_id: Optional[int] = None,
) -> Tuple[Optional[str], Optional[List[str]]]:
    """校验规则并返回 (单值范围, 多值范围)。

    v1.35.0 起一条规则可覆盖多个分组/设备/标签，值之间为「或」；
    `scope_value`（v1.34.0 单值字段）保留兼容，传数组时以数组为准，
    单值字段同步存首个值，供旧客户端与 downgrade 使用。
    """
    if metric not in rules.METRICS:
        raise RuleValidationError(f"不支持的指标：{metric}")
    if level not in rules.LEVELS:
        raise RuleValidationError("告警级别只能是 warning 或 critical")
    _validate_direction(metric, op)
    _validate_range(metric, threshold)

    resolved_value: Optional[str] = None
    resolved_values: Optional[List[str]] = None
    if scope_type == "global":
        resolved_value, resolved_values = None, None
    elif scope_type in ("group", "device", "tag"):
        raw_values = list(scope_values or [])
        if not raw_values and scope_value:
            raw_values = [scope_value]
        if not raw_values:
            label = {"group": "分组名", "device": "设备 ID", "tag": "标签"}[scope_type]
            raise RuleValidationError(f"生效范围为「指定{label}」时必须填写至少一个{label}")
        if len(raw_values) > 50:
            raise RuleValidationError("单条规则的生效范围最多 50 个值")

        cleaned: List[str] = []
        seen = set()
        for item in raw_values:
            text = (item or "").strip()
            if scope_type == "tag":
                try:
                    text = tag_service.normalize_tag(text)
                except tag_service.TagValidationError as exc:
                    raise RuleValidationError(f"标签不合法：{exc}") from exc
            elif not text:
                raise RuleValidationError("生效范围不能包含空值")
            elif len(text) > 64:
                raise RuleValidationError("生效范围单个值最多 64 个字符")
            if text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        if not cleaned:
            raise RuleValidationError("生效范围不能为空")
        resolved_value, resolved_values = cleaned[0], cleaned
    else:
        raise RuleValidationError("生效范围只能是 global / group / device / tag")

    await _validate_consistency(
        session,
        metric=metric,
        op=op,
        level=level,
        scope_type=scope_type,
        scope_value=resolved_value,
        threshold=threshold,
        exclude_id=exclude_id,
    )
    return resolved_value, resolved_values


async def _sync_rule_config(session: AsyncSession, rule: AlertRule, operator: str) -> None:
    """默认规则的阈值镜像回 rule_config：downgrade 回 v1.33.0 仍保留最近阈值。"""
    key = _config_key(rule)
    if key is None:
        return
    row = await session.get(RuleConfig, key)
    if row is None:
        session.add(RuleConfig(key=key, value=rule.threshold, updated_by=operator))
    else:
        row.value = rule.threshold
        row.updated_by = operator
    rules.patch_override(key, float(rule.threshold))


async def create_rule(
    session: AsyncSession, req: AlertRuleCreate, operator: str
) -> AlertRule:
    scope_value, scope_values = await _validate(
        session,
        metric=req.metric,
        op=req.op,
        level=req.level,
        threshold=req.threshold,
        scope_type=req.scope_type,
        scope_value=req.scope_value,
        scope_values=req.scope_values,
    )
    rule = AlertRule(
        name=req.name.strip(),
        metric=req.metric,
        op=req.op,
        threshold=req.threshold,
        level=req.level,
        scope_type=req.scope_type,
        scope_value=scope_value,
        scope_values=scope_values,
        enabled=req.enabled,
        consecutive_count=req.consecutive_count,
        priority=req.priority,
        is_default=False,
        updated_by=operator,
    )
    session.add(rule)
    await session.flush()
    await load_rules(session)
    return rule


def to_jsonable(value: Any) -> Any:
    """审计 detail 必须可 JSON 序列化：Decimal 转 float、时间转 ISO 字符串。"""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


async def update_rule(
    session: AsyncSession, rule_id: int, req: AlertRuleUpdate, operator: str
) -> Tuple[AlertRule, List[str]]:
    rule = await session.get(AlertRule, rule_id)
    if rule is None:
        raise LookupError("告警规则不存在")

    payload = req.model_dump(exclude_unset=True)
    # scope_value / scope_values 显式传 null 表示「保持原值」，传空串/空数组才表示清空
    if "scope_value" in payload and payload["scope_value"] is None:
        payload.pop("scope_value")
    if "scope_values" in payload and payload["scope_values"] is None:
        payload.pop("scope_values")
    if "name" in payload and payload["name"] is not None:
        payload["name"] = payload["name"].strip()

    merged = {
        "name": payload.get("name", rule.name),
        "metric": payload.get("metric", rule.metric),
        "op": payload.get("op", rule.op),
        "level": payload.get("level", rule.level),
        "threshold": payload.get("threshold", float(rule.threshold)),
        "scope_type": payload.get("scope_type", rule.scope_type),
        "consecutive_count": payload.get("consecutive_count", rule.consecutive_count),
        "priority": payload.get("priority", rule.priority),
        "enabled": payload.get("enabled", rule.enabled),
    }
    if not merged["name"]:
        raise RuleValidationError("规则名称不能为空")

    # 范围取值优先级：显式 scope_values > 显式 scope_value（旧客户端） > 保持现状
    if "scope_values" in payload:
        scope_values_input = payload["scope_values"]
        scope_value_input = None
    elif "scope_value" in payload:
        scope_values_input = None
        scope_value_input = payload["scope_value"]
    else:
        scope_values_input = _scope_values_of(rule)
        scope_value_input = None

    scope_value, scope_values = await _validate(
        session,
        metric=merged["metric"],
        op=merged["op"],
        level=merged["level"],
        threshold=merged["threshold"],
        scope_type=merged["scope_type"],
        scope_value=scope_value_input,
        scope_values=scope_values_input,
        exclude_id=rule.id,
    )

    old_scope_values = _scope_values_of(rule)
    old_scope_value = rule.scope_value
    changes: Dict[str, Dict[str, Any]] = {}
    changed: List[str] = []
    for field, value in merged.items():
        before = getattr(rule, field)
        if before != value:
            setattr(rule, field, value)
            changes[field] = {"from": to_jsonable(before), "to": to_jsonable(value)}
            changed.append(field)
    if (old_scope_value or None) != (scope_value or None):
        rule.scope_value = scope_value
        changes["scope_value"] = {"from": old_scope_value, "to": scope_value}
        changed.append("scope_value")
    if (old_scope_values or None) != (scope_values or None):
        rule.scope_values = scope_values
        changes["scope_values"] = {"from": old_scope_values or None, "to": scope_values}
        changed.append("scope_values")

    rule.updated_by = operator
    await session.flush()

    if "threshold" in changed:
        await _sync_rule_config(session, rule, operator)
    await load_rules(session)
    return rule, changed, changes


async def set_enabled(
    session: AsyncSession, rule_id: int, enabled: bool, operator: str
) -> Tuple[AlertRule, bool]:
    rule = await session.get(AlertRule, rule_id)
    if rule is None:
        raise LookupError("告警规则不存在")
    before = bool(rule.enabled)
    rule.enabled = enabled
    rule.updated_by = operator
    await session.flush()
    await load_rules(session)
    return rule, before


async def delete_rule(session: AsyncSession, rule_id: int) -> AlertRule:
    rule = await session.get(AlertRule, rule_id)
    if rule is None:
        raise LookupError("告警规则不存在")
    await session.delete(rule)
    await session.flush()
    await load_rules(session)
    return rule


async def restore_defaults(session: AsyncSession, operator: str) -> List[Dict[str, Any]]:
    """把默认规则的阈值重置为 Settings 值（自定义规则保持不动）。

    用于运维「改阈值后结果不对，回滚到出厂值」；返回逐规则的前后值，
    由路由写入审计，规则变更历史因此能看到「从多少改回多少」。
    """
    rows = list((await session.execute(select(AlertRule).where(AlertRule.is_default.is_(True)))).scalars().all())
    changes: List[Dict[str, Any]] = []
    for row in rows:
        key = _config_key(row)
        if key is None:
            continue
        target = float(getattr(get_settings(), key))
        if float(row.threshold) != target:
            changes.append(
                {
                    "rule_id": row.id,
                    "name": row.name,
                    "metric": row.metric,
                    "from": float(row.threshold),
                    "to": target,
                }
            )
            row.threshold = target
            row.updated_by = operator
        config_row = await session.get(RuleConfig, key)
        if config_row is not None:
            await session.delete(config_row)
        rules.clear_override(key)
    await session.flush()
    await load_rules(session)
    return changes


async def apply_threshold_overrides(
    session: AsyncSession, updates: Dict[str, float], operator: str
) -> None:
    """旧 /api/rules/thresholds 写入后，把新阈值同步到对应的默认规则。

    v1.34.0 起判定读 alert_rule 表，若只写 rule_config 会导致重启后丢失修改，
    因此旧接口必须同时更新默认规则行，力保「旧客户端行为不变」。
    """
    specs = {spec["config_key"]: spec for spec in rules.DEFAULT_RULE_SPECS}
    for key, value in updates.items():
        spec = specs.get(key)
        if spec is None:
            continue
        stmt = select(AlertRule).where(
            AlertRule.is_default.is_(True),
            AlertRule.metric == spec["metric"],
            AlertRule.op == spec["op"],
            AlertRule.level == spec["level"],
            AlertRule.scope_type == "global",
        )
        for row in (await session.execute(stmt)).scalars().all():
            row.threshold = value
            row.updated_by = operator
    await session.flush()
    await load_rules(session)
