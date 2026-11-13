"""告警规则条件树（v1.38.0）。

- `alert_rule` 新增 `conditions`（JSON/JSONB）：支持 all（且）/any（或）嵌套，
  叶子维度为 group / device / tag；
- 既有规则在迁移中等价转换：`scope_type` 为 group/device/tag 的行写入
  `{"op":"any","children":[{"dim":<scope_type>,"values":<scope_values 或 [scope_value]>}]}`。
  这样老规则在 v1.38 走条件树分支时的匹配结果与 v1.35 完全一致；
- **不清空、不改写** `scope_type / scope_value / scope_values`：downgrade 回 v1.35 后
  老字段仍可读、可继续匹配，只有真正嵌套的条件树（scope_type=custom）在 v1.35 下
  不命中（fail-closed）；
- global 规则的 `conditions` 保持 NULL，表示无范围限制（「空树命中一切」被禁止）。

Revision ID: 0013_rule_conditions
Revises: 0012_shadow_history
Create Date: 2026-11-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0013_rule_conditions"
down_revision: Union[str, None] = "0012_shadow_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _backfill_conditions() -> None:
    """把 v1.35 的单维度多值范围等价转换为条件树（单叶子 any）。

    仅处理非 global 且尚未写条件树的行；老字段原样保留，供 downgrade 使用。
    """
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "UPDATE alert_rule SET conditions = jsonb_build_object("
                "  'op', 'any',"
                "  'children', jsonb_build_array(jsonb_build_object("
                "     'dim', scope_type,"
                "     'values', COALESCE(scope_values, jsonb_build_array(scope_value))"
                "  ))"
                ") WHERE scope_type IN ('group', 'device', 'tag') AND conditions IS NULL"
            )
        )
    else:
        # SQLite：json() 防止从列取出的 JSON 被再次转义成字符串
        bind.execute(
            sa.text(
                "UPDATE alert_rule SET conditions = json_object("
                "  'op', 'any',"
                "  'children', json_array(json_object("
                "     'dim', scope_type,"
                "     'values', json(COALESCE(scope_values, json_array(scope_value)))"
                "  ))"
                ") WHERE scope_type IN ('group', 'device', 'tag') AND conditions IS NULL"
            )
        )


def upgrade() -> None:
    tables = _tables()
    if "alert_rule" in tables and "conditions" not in _columns("alert_rule"):
        op.add_column(
            "alert_rule",
            sa.Column("conditions", sa.JSON().with_variant(JSONB, "postgresql"), nullable=True),
        )
    if "alert_rule" in _tables():
        _backfill_conditions()


def downgrade() -> None:
    if "alert_rule" in _tables() and "conditions" in _columns("alert_rule"):
        with op.batch_alter_table("alert_rule") as batch:
            batch.drop_column("conditions")
