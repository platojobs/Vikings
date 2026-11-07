"""设备标签体系与规则多值范围（v1.35.0）。

- 新建 `device_tag` 关联表：`(device_id, tag)` 复合主键保证同一设备同一标签唯一，
  `(tag, device_id)` 索引支撑按标签筛设备与标签计数；
- `alert_rule` 新增 `scope_values`（JSON 数组）：规则可覆盖多个分组/设备/标签，
  值之间为「或」；既有单值规则在迁移中回填为 `[scope_value]`，行为不变；
  `scope_value` 保留（存首个值）以兼容 v1.34.0 客户端与 downgrade。

Revision ID: 0010_device_tags
Revises: 0009_alert_rules
Create Date: 2026-11-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010_device_tags"
down_revision: Union[str, None] = "0009_alert_rules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _backfill_scope_values() -> None:
    """把 v1.34.0 的单值范围回填为数组，保证老规则匹配语义不变。"""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "UPDATE alert_rule SET scope_values = jsonb_build_array(scope_value) "
                "WHERE scope_type <> 'global' AND scope_value IS NOT NULL "
                "AND scope_values IS NULL"
            )
        )
    else:
        bind.execute(
            sa.text(
                "UPDATE alert_rule SET scope_values = json_array(scope_value) "
                "WHERE scope_type <> 'global' AND scope_value IS NOT NULL "
                "AND scope_values IS NULL"
            )
        )


def upgrade() -> None:
    tables = _tables()

    if "device_tag" not in tables:
        op.create_table(
            "device_tag",
            sa.Column("device_id", sa.String(64), primary_key=True),
            sa.Column("tag", sa.String(32), primary_key=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
            sa.Column(
                "created_by", sa.String(64), nullable=False, server_default="system"
            ),
        )
        op.create_index("idx_device_tag_tag", "device_tag", ["tag", "device_id"])

    if "alert_rule" in tables and "scope_values" not in _columns("alert_rule"):
        op.add_column(
            "alert_rule",
            sa.Column("scope_values", sa.JSON().with_variant(JSONB, "postgresql"), nullable=True),
        )
    if "alert_rule" in _tables():
        _backfill_scope_values()


def downgrade() -> None:
    tables = _tables()

    if "alert_rule" in tables and "scope_values" in _columns("alert_rule"):
        with op.batch_alter_table("alert_rule") as batch:
            batch.drop_column("scope_values")

    if "device_tag" in tables:
        indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("device_tag")}
        if "idx_device_tag_tag" in indexes:
            op.drop_index("idx_device_tag_tag", table_name="device_tag")
        op.drop_table("device_tag")
