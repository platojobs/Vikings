"""可配置告警阈值规则（v1.34.0）。

- 新建 `alert_rule` 表：指标 / 比较方向 / 阈值 / 级别 / 生效范围 / 启用 /
  连续次数 / 优先级 / 是否默认；
- 种子：把 v1.33.0 的 10 个硬编码阈值（电压 2、温度 4、湿度 2、信号 2）
  写成 `is_default=True` 的默认规则，范围 global、连续次数 1；
  阈值优先取 `rule_config` 的在线覆盖值，没有则取 v1.33.0 的默认值，
  从而保证升级后同一份上报数据的告警结果与升级前一致；
- `device_alert` 新增 `matched_rule`（JSON）：记录命中的规则快照；
  历史告警保持 NULL（v1.34.0 之前没有规则概念，不做臆测回填）。

Revision ID: 0009_alert_rules
Revises: 0008_alert_sla_handover
Create Date: 2026-11-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0009_alert_rules"
down_revision: Union[str, None] = "0008_alert_sla_handover"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (config_key, name, metric, op, level, priority, v1.33.0 默认值)
DEFAULT_RULES = (
    ("threshold_voltage_critical", "电压过低-严重", "voltage", "lt", "critical", 90, "3.20"),
    ("threshold_voltage_warning", "电压过低-警告", "voltage", "lt", "warning", 100, "3.40"),
    ("threshold_temperature_critical_high", "温度过高-严重", "temperature", "gt", "critical", 90, "75.00"),
    ("threshold_temperature_warning_high", "温度过高-警告", "temperature", "gt", "warning", 100, "60.00"),
    ("threshold_temperature_critical_low", "温度过低-严重", "temperature", "lt", "critical", 90, "-20.00"),
    ("threshold_temperature_warning_low", "温度过低-警告", "temperature", "lt", "warning", 100, "-10.00"),
    ("threshold_humidity_critical", "湿度偏高-严重", "humidity", "gt", "critical", 90, "97.00"),
    ("threshold_humidity_warning", "湿度偏高-警告", "humidity", "gt", "warning", 100, "90.00"),
    ("threshold_rssi_critical", "信号过弱-严重", "rssi", "lt", "critical", 90, "-110.00"),
    ("threshold_rssi_warning", "信号过弱-警告", "rssi", "lt", "warning", 100, "-100.00"),
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _overrides() -> dict[str, str]:
    """读取 v1.33.0 的在线覆盖值；表不存在时返回空（用默认值）。"""
    if "rule_config" not in _tables():
        return {}
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT key, value FROM rule_config")).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def _seed_defaults() -> None:
    bind = op.get_bind()
    total = bind.execute(sa.text("SELECT COUNT(*) FROM alert_rule")).scalar() or 0
    if int(total) > 0:
        return
    overrides = _overrides()
    for config_key, name, metric, compare_op, level, priority, fallback in DEFAULT_RULES:
        value = overrides.get(config_key, fallback)
        bind.execute(
            sa.text(
                "INSERT INTO alert_rule "
                "(name, metric, op, threshold, level, scope_type, scope_value, enabled, "
                " consecutive_count, priority, is_default, updated_by) "
                "VALUES (:name, :metric, :op, :threshold, :level, 'global', NULL, TRUE, "
                " 1, :priority, TRUE, 'system')"
            ),
            {
                "name": name,
                "metric": metric,
                "op": compare_op,
                "threshold": value,
                "level": level,
                "priority": priority,
            },
        )


def upgrade() -> None:
    tables = _tables()

    if "alert_rule" not in tables:
        op.create_table(
            "alert_rule",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("metric", sa.String(32), nullable=False),
            sa.Column("op", sa.String(8), nullable=False),
            sa.Column("threshold", sa.Numeric(10, 2), nullable=False),
            sa.Column("level", sa.String(16), nullable=False),
            sa.Column("scope_type", sa.String(16), nullable=False, server_default="global"),
            sa.Column("scope_value", sa.String(64), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("consecutive_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_by", sa.String(64), nullable=False, server_default="system"),
        )
        op.create_index("idx_alert_rule_metric", "alert_rule", ["metric"])
        op.create_index("idx_alert_rule_scope", "alert_rule", ["scope_type", "scope_value"])

    if "alert_rule" in _tables():
        _seed_defaults()

    if "device_alert" in tables and "matched_rule" not in _columns("device_alert"):
        op.add_column(
            "device_alert",
            sa.Column("matched_rule", sa.JSON().with_variant(JSONB, "postgresql"), nullable=True),
        )


def downgrade() -> None:
    tables = _tables()

    if "device_alert" in tables and "matched_rule" in _columns("device_alert"):
        with op.batch_alter_table("device_alert") as batch:
            batch.drop_column("matched_rule")

    if "alert_rule" in tables:
        indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("alert_rule")}
        for name in ("idx_alert_rule_metric", "idx_alert_rule_scope"):
            if name in indexes:
                op.drop_index(name, table_name="alert_rule")
        op.drop_table("alert_rule")
